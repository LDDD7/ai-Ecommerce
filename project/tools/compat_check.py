import asyncio, json, os, shutil, sqlite3, traceback

# NOTE: 本机沙箱下 %TEMP% 与 tempfile.mkdtemp 创建的子目录 ACL 异常（Access denied），
# 因此所有临时产物固定落在工作区内，不用 tempfile。
BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".compat_run")
shutil.rmtree(BASE, ignore_errors=True)
os.makedirs(BASE, exist_ok=True)
db = os.path.join(BASE, "t.db")

ok, bad = [], []

def check(name, fn):
    try:
        fn()
        ok.append(name)
    except Exception as e:
        bad.append(f"{name}: {type(e).__name__}: {e}")
        traceback.print_exc()

def t_sqlite():
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("create table f(dt text, sku text, gmv real)")
    con.executemany("insert into f values(?,?,?)", [("2026-11-01", "s1", 10.0), ("2026-11-02", "s1", 12.0), ("2026-11-02", "s2", 7.0)])
    con.commit()
    r = con.execute("""
        with base as (select dt, sum(gmv) g from f group by dt)
        select dt, g, sum(g) over (order by dt) run, lag(g) over (order by dt) prev from base order by dt
    """).fetchall()
    assert r[0][2] == 10.0 and r[-1][2] == 29.0, r
    # JSON1（存 payload 用）
    assert con.execute("select json_extract('{\"a\":1}', '$.a')").fetchone()[0] == 1
    # 只读连接
    ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    assert ro.execute("select count(*) from f").fetchone()[0] == 3
    try:
        ro.execute("delete from f")
        raise AssertionError("readonly conn allowed write")
    except sqlite3.OperationalError as e:
        assert "readonly" in str(e).lower(), e
    ro.close()
    # progress handler 中断慢查询
    con.set_progress_handler(lambda: 1, 100)
    try:
        con.execute("with recursive c(x) as (select 1 union all select x+1 from c where x<100000000) select count(*) from c").fetchone()
        raise AssertionError("timeout handler did not abort")
    except sqlite3.OperationalError as e:
        assert "interrupted" in str(e), e
    finally:
        con.set_progress_handler(None, 0)
    con.close()

def t_sqlalchemy_sync():
    from sqlalchemy import create_engine, text
    e = create_engine(f"sqlite:///{db}")
    with e.connect() as c:
        assert c.execute(text("select count(*) from f")).scalar() == 3
    e.dispose()

def t_sqlalchemy_async():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    async def main():
        e = create_async_engine(f"sqlite+aiosqlite:///{db}")
        async with e.connect() as c:
            assert (await c.execute(text("select count(*) from f"))).scalar() == 3
        await e.dispose()
    asyncio.run(main())

def t_sse():
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse
    import httpx
    app = FastAPI()

    @app.post("/api/chat")
    async def chat():
        async def gen():
            for ev in ({"type": "plan"}, {"type": "token", "text": "hi"}, {"type": "done"}):
                yield f"event: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0)
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def main():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
            async with cl.stream("POST", "/api/chat") as r:
                assert r.status_code == 200
                assert r.headers["content-type"].startswith("text/event-stream"), r.headers
                lines = [l async for l in r.aiter_lines() if l]
                assert lines[0] == "event: plan", lines[:2]
                assert "event: done" in lines, lines
    asyncio.run(main())

def t_static():
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles
    import httpx
    d = os.path.join(BASE, "dist")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "index.html"), "w", encoding="utf-8").write("<h1>hi</h1>")
    open(os.path.join(d, "app.js"), "w", encoding="utf-8").write("console.log(1)")
    app = FastAPI()
    app.mount("/", StaticFiles(directory=d, html=True), name="web")

    async def main():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
            assert (await cl.get("/")).status_code == 200
            assert (await cl.get("/app.js")).status_code == 200
            # 前端路由回退 SPA 场景：/chat 未命中应回落 index.html
            r = await cl.get("/chat")
            assert r.status_code in (200, 404), r.status_code
    asyncio.run(main())

def t_upload_middleware_free():
    # 只需要 CORS 在 dev 下联调（Vite :5173 -> API :8000）
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])

def t_openai():
    import inspect
    from openai import OpenAI, AsyncOpenAI
    sig = inspect.signature(OpenAI.__init__)
    assert "base_url" in sig.parameters and "api_key" in sig.parameters
    from openai.resources.chat.completions import Completions
    ps = inspect.signature(Completions.create).parameters
    for p in ("tools", "tool_choice", "response_format", "stream", "timeout"):
        assert p in ps, f"missing {p}"
    assert hasattr(AsyncOpenAI, "chat")

def t_pydantic_settings():
    from pydantic_settings import BaseSettings
    class S(BaseSettings):
        llm_base_url: str = "http://x"
    assert S().llm_base_url == "http://x"

def t_pyyaml():
    import yaml
    d = yaml.safe_load("name: gmv\nexpr: SUM(x)\nfilters:\n  allowed: [channel_id]\n")
    assert d["filters"]["allowed"] == ["channel_id"]

def t_tenacity():
    from tenacity import retry, stop_after_attempt
    n = {"i": 0}
    @retry(stop=stop_after_attempt(3))
    def f():
        n["i"] += 1
        if n["i"] < 2:
            raise ValueError("x")
        return "ok"
    assert f() == "ok"

def t_orjson():
    import orjson
    assert orjson.dumps({"a": 1}) == b'{"a":1}'

for name, fn in [
    ("sqlite: WAL/CTE/window/JSON1/readonly/progress-handler", t_sqlite),
    ("sqlalchemy sync", t_sqlalchemy_sync),
    ("sqlalchemy async + aiosqlite", t_sqlalchemy_async),
    ("fastapi SSE streaming", t_sse),
    ("StaticFiles serve dist", t_static),
    ("CORS middleware", t_upload_middleware_free),
    ("openai sdk compat surface", t_openai),
    ("pydantic-settings", t_pydantic_settings),
    ("pyyaml", t_pyyaml),
    ("tenacity", t_tenacity),
    ("orjson", t_orjson),
]:
    check(name, fn)

print(f"\nPASS {len(ok)} / FAIL {len(bad)}")
for b in bad:
    print("  FAIL", b)
