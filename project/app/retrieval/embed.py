"""本地向量化（bge-m3，1024 维）+ SQLite BLOB 存储。

设计取舍（spec §20.1）：语料只有几十条，**不引向量库**——37×1024×4 ≈ 148KB，
存 SQLite、取出来 numpy 做余弦就够，省掉一个常驻进程和一套依赖。

模型加载很贵（实测 47s、RSS 1.9GB），所以：
- 首次 `encode()` 才加载，进程内单例，带锁；
- 加载失败或 `EMBED_MODE=off` 时 `available()` 为 False，检索自动退化成 FTS 单路；
- 路径兼容「普通目录」和「HF cache 的 snapshots/<hash>」两种布局。
"""

from __future__ import annotations

import hashlib
import os
import threading
from datetime import datetime
from pathlib import Path

import numpy as np

from app.core.config import get_settings
from app.retrieval.texts import Doc

DEFAULT_MODEL = "BAAI/bge-m3"
DEFAULT_DIM = 1024

_LOCK = threading.Lock()
_MODEL = None
_FAILED = False
_MODEL_CACHE: dict[str, object] = {}


# --------------------------------------------------------------------------- #
# 路径与模型
# --------------------------------------------------------------------------- #
def resolve_model_path(raw: str | Path | None) -> Path | None:
    """兼容「模型目录」与「HF cache 根（含 snapshots/）」两种布局。"""
    if not raw:
        return None
    path = Path(raw)
    if not path.is_dir():
        return None
    if (path / "config.json").exists():
        return path
    snapshots = path / "snapshots"
    if snapshots.is_dir():
        for snap in sorted(snapshots.iterdir(), reverse=True):
            if (snap / "config.json").exists():
                return snap
    return None


def model_name() -> str:
    settings = get_settings()
    raw = getattr(settings, "embed_model_path", "") or ""
    path = resolve_model_path(raw)
    return path.parent.name if path and path.parent.name.startswith("models--") else DEFAULT_MODEL


def _load():
    global _MODEL, _FAILED
    if _MODEL is not None or _FAILED:
        return _MODEL
    with _LOCK:
        if _MODEL is not None or _FAILED:
            return _MODEL
        settings = get_settings()
        if getattr(settings, "embed_mode", "local") == "off":
            _FAILED = True
            return None
        path = resolve_model_path(getattr(settings, "embed_model_path", ""))
        if path is None:
            _FAILED = True
            return None
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        try:
            import torch
            from sentence_transformers import SentenceTransformer

            torch.set_num_threads(min(8, os.cpu_count() or 4))
            _MODEL = SentenceTransformer(str(path), device="cpu")
        except Exception:
            _FAILED = True
            _MODEL = None
        return _MODEL


def available() -> bool:
    return _load() is not None


def reset_cache() -> None:
    """测试用：清掉单例与失败标记。"""
    global _MODEL, _FAILED
    with _LOCK:
        _MODEL = None
        _FAILED = False


def encode(texts: list[str]) -> np.ndarray | None:
    """归一化向量，(n, dim) float32；不可用时返回 None。"""
    model = _load()
    if model is None or not texts:
        return None
    vectors = model.encode(
        texts,
        batch_size=16,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    arr = np.asarray(vectors, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    return _normalize(arr)


def _normalize(arr: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (arr / norms).astype(np.float32)


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #
def text_hash(text: str, model: str | None = None) -> str:
    name = model or model_name()
    return hashlib.sha256(f"{name}|{text}".encode("utf-8")).hexdigest()


def _connect(db_path: str | Path):
    import sqlite3

    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"数据文件不存在：{path}")
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    return con


def _ensure_ddl(con) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS embedding (
            owner_table TEXT NOT NULL,
            owner_id    TEXT NOT NULL,
            model       TEXT NOT NULL,
            dim         INTEGER NOT NULL,
            vec         BLOB NOT NULL,
            text_hash   TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (owner_table, owner_id, model)
        );
        """
    )


def _existing(con, owner_table: str, owner_id: str, model: str) -> str | None:
    row = con.execute(
        "SELECT text_hash FROM embedding WHERE owner_table = ? AND owner_id = ? AND model = ?",
        (owner_table, owner_id, model),
    ).fetchone()
    return None if row is None else str(row["text_hash"])


def ensure_index(db_path: str | Path, docs: list[Doc], force: bool = False) -> dict:
    """给缺向量或文本变了的文档补算向量。返回 {model, embedded, skipped, dim}。"""
    if not docs:
        return {"model": model_name(), "embedded": 0, "skipped": 0, "dim": 0}
    model = model_name()
    con = _connect(db_path)
    try:
        _ensure_ddl(con)
        todo: list[Doc] = []
        for doc in docs:
            if force or _existing(con, doc.owner_table, doc.owner_id, model) != text_hash(doc.index_text(), model):
                todo.append(doc)
        if not todo:
            return {"model": model, "embedded": 0, "skipped": len(docs), "dim": 0}
        vectors = encode([d.index_text() for d in todo])
        if vectors is None:
            return {"model": model, "embedded": 0, "skipped": len(docs), "dim": 0, "reason": "embedding unavailable"}
        now = datetime.now().isoformat(timespec="seconds")
        dim = int(vectors.shape[1])
        con.executemany(
            "INSERT OR REPLACE INTO embedding (owner_table, owner_id, model, dim, vec, text_hash, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (d.owner_table, d.owner_id, model, dim, vectors[i].tobytes(), text_hash(d.index_text(), model), now)
                for i, d in enumerate(todo)
            ],
        )
        con.commit()
        return {"model": model, "embedded": len(todo), "skipped": len(docs) - len(todo), "dim": dim}
    finally:
        con.close()


def load_matrix(db_path: str | Path, owner_table: str | None = None) -> tuple[list[tuple[str, str]], np.ndarray | None]:
    """读出所有已索引向量；返回 (keys, matrix)。没有数据时 matrix 为 None。"""
    model = model_name()
    con = _connect(db_path)
    try:
        _ensure_ddl(con)
        sql = "SELECT owner_table, owner_id, dim, vec FROM embedding WHERE model = ?"
        params: list[object] = [model]
        if owner_table:
            sql += " AND owner_table = ?"
            params.append(owner_table)
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()
    keys: list[tuple[str, str]] = []
    vectors: list[np.ndarray] = []
    for row in rows:
        dim = int(row["dim"])
        vec = np.frombuffer(row["vec"], dtype=np.float32)
        if vec.size != dim:
            continue
        keys.append((str(row["owner_table"]), str(row["owner_id"])))
        vectors.append(vec)
    if not vectors:
        return [], None
    return keys, _normalize(np.vstack(vectors))


def similarity(query_vec: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    q = query_vec.astype(np.float32).reshape(-1)
    q = q / (np.linalg.norm(q) or 1.0)
    return matrix @ q
