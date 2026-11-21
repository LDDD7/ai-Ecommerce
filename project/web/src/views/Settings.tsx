import { useCallback, useEffect, useMemo, useState } from "react";

import {
  ApiError,
  getSettings,
  resetSettings,
  saveSettings,
  testSettings,
  type ProviderPreset,
  type SessionUser,
  type SettingsSnapshot,
  type SettingsTestResult,
} from "../api";
import { Accounts } from "./Accounts";
import { PromoCalendar } from "./PromoCalendar";

/** 表单状态：全部是字符串，后端按「空串=删除覆盖」处理。 */
type Draft = Record<string, string>;

const CHAT_FIELDS = [
  "llm_base_url",
  "llm_api_key",
  "llm_model_planner",
  "llm_model_fast",
  "llm_model_vision",
  "llm_mode",
] as const;

const CREATIVE_FIELDS = [
  "creative_provider",
  "dashscope_api_key",
  "creative_image_model",
  "creative_edit_model",
  "creative_adapter",
] as const;

const SECRET_FIELDS = new Set(["llm_api_key", "dashscope_api_key"]);

function draftFrom(snap: SettingsSnapshot): Draft {
  const draft: Draft = {};
  for (const [name, field] of Object.entries(snap.fields)) {
    // secret 字段后端不回明文：留空表示「不修改」，用户想换就整条重填
    draft[name] = field.secret ? "" : field.value;
  }
  return draft;
}

function sourceLabel(source: SettingsSnapshot["fields"][string]["source"]): string {
  if (source === "db") return "页面设置";
  if (source === "env") return ".env";
  return "未填";
}

export default function Settings({ me }: { me: SessionUser }) {
  const [snap, setSnap] = useState<SettingsSnapshot | null>(null);
  const [draft, setDraft] = useState<Draft>({});
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ tone: "ok" | "bad" | "flat"; text: string } | null>(null);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const next = await getSettings();
      setSnap(next);
      setDraft(draftFrom(next));
    } catch (err) {
      setNote({ tone: "bad", text: err instanceof ApiError ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const providers = snap?.providers ?? [];
  const creativeProviders = snap?.creative_providers ?? [];
  const activeProvider = useMemo(
    () => providers.find((p) => p.base_url && p.base_url === (draft.llm_base_url ?? "").trim()),
    [providers, draft.llm_base_url],
  );
  const activeCreative = useMemo(
    () => creativeProviders.find((p) => p.id === (draft.creative_provider ?? "local")),
    [creativeProviders, draft.creative_provider],
  );

  const set = (name: string, value: string) => setDraft((prev) => ({ ...prev, [name]: value }));

  /** 换服务商：把地址和空着的模型一起换成预设值（已填的模型不覆盖，避免手改丢失）。 */
  const pickProvider = (preset: ProviderPreset) => {
    setDraft((prev) => {
      const next: Draft = { ...prev, llm_base_url: preset.base_url };
      const [first] = preset.models;
      if (first) {
        if (!prev.llm_model_planner?.trim()) next.llm_model_planner = first;
        if (!prev.llm_model_fast?.trim()) next.llm_model_fast = first;
        if (!prev.llm_model_vision?.trim()) next.llm_model_vision = first;
      }
      return next;
    });
  };

  const pickCreative = (preset: ProviderPreset) => {
    setDraft((prev) => {
      const next: Draft = { ...prev, creative_provider: preset.id };
      const [image, edit] = preset.models;
      if (image && !prev.creative_image_model?.trim()) next.creative_image_model = image;
      if (edit && !prev.creative_edit_model?.trim()) next.creative_edit_model = edit;
      return next;
    });
  };

  const patch = (): Record<string, string> => {
    const body: Record<string, string> = {};
    for (const name of [...CHAT_FIELDS, ...CREATIVE_FIELDS]) {
      const value = (draft[name] ?? "").trim();
      // secret 留空 = 不改（否则一次保存就把 key 删了）
      if (SECRET_FIELDS.has(name) && !value) continue;
      body[name] = value;
    }
    return body;
  };

  const onSave = async () => {
    setBusy(true);
    setNote(null);
    try {
      const body = patch();
      if (Object.keys(body).length === 0) {
        setNote({ tone: "flat", text: "没有改动" });
        return;
      }
      const next = await saveSettings(body);
      setSnap(next);
      setDraft(draftFrom(next));
      setNote({ tone: "ok", text: "已保存（下一次请求立即生效，无需重启）" });
    } catch (err) {
      setNote({ tone: "bad", text: err instanceof ApiError ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  const onReset = async () => {
    setBusy(true);
    setNote(null);
    try {
      const next = await resetSettings();
      setSnap(next);
      setDraft(draftFrom(next));
      setNote({ tone: "ok", text: "已恢复 .env 默认值" });
    } catch (err) {
      setNote({ tone: "bad", text: err instanceof ApiError ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  const onTest = async (target: "chat" | "creative") => {
    setBusy(true);
    setNote({ tone: "flat", text: "正在测试…" });
    try {
      const chatBase = (draft.llm_base_url ?? "").trim();
      const chatModel = (draft.llm_model_fast ?? "").trim();
      const creativeModel = (draft.creative_image_model ?? "").trim();
      const result: SettingsTestResult = await testSettings(
        target === "chat"
          ? {
              target,
              base_url: chatBase || undefined,
              api_key: (draft.llm_api_key ?? "").trim() || undefined,
              model: chatModel || undefined,
            }
          : { target, api_key: (draft.dashscope_api_key ?? "").trim() || undefined },
      );
      if (result.ok) {
        setNote({
          tone: "ok",
          text: `连接成功 · ${result.model ?? ""} · ${result.elapsed_ms ?? 0}ms${
            target === "creative" ? `（${creativeModel || "默认生图模型"} 仅做 key 校验）` : ""
          }`,
        });
      } else {
        setNote({ tone: "bad", text: result.reason ?? "测试失败" });
      }
    } catch (err) {
      setNote({ tone: "bad", text: err instanceof ApiError ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  if (!snap) {
    return <p className="hint">{busy ? "正在读取设置…" : "读取设置失败，请刷新重试"}</p>;
  }

  const field = (name: string) => snap.fields[name];
  const value = (name: string) => draft[name] ?? "";
  const datalist = (id: string, models: string[]) => (
    <datalist id={id}>
      {models.map((m) => (
        <option key={m} value={m} />
      ))}
    </datalist>
  );

  return (
    <section className="settings">
      <header className="settings__head">
        <h2>设置</h2>
        <p className="hint">
          Key 只保存在本机数据库（<code>{snap.env_file.replace(/[\\/]\.env$/, "")}</code> 的{" "}
          <code>app_setting</code> 表），不会上传、不会回传到浏览器；页面上只会看到掩码。
        </p>
      </header>

      <div className="settings__status">
        <span className={`pill ${snap.llm.active === "real" ? "pill--ok" : "pill--flat"}`}>
          对话模型：{snap.llm.active === "real" ? "真实调用" : "模拟（规则版）"}
        </span>
        <span className={`pill ${snap.creative.provider === "local" ? "pill--flat" : "pill--ok"}`}>
          生图：{snap.creative.provider === "local" ? "本地 PIL" : "百炼"}
        </span>
        {snap.overrides.length > 0 && (
          <span className="pill pill--flat">已覆盖 {snap.overrides.length} 项（.env 之上）</span>
        )}
      </div>

      <fieldset className="settings__group" disabled={busy}>
        <legend>对话模型</legend>

        <div className="settings__providers">
          {providers.map((p) => (
            <button
              key={p.id}
              type="button"
              className={`preset ${activeProvider?.id === p.id ? "preset--on" : ""}`}
              onClick={() => pickProvider(p)}
              title={p.note}
            >
              {p.label}
            </button>
          ))}
        </div>

        <label className="field">
          <span>
            {field("llm_base_url").label}
            <em className={`src src--${field("llm_base_url").source}`}>{sourceLabel(field("llm_base_url").source)}</em>
          </span>
          <input
            type="url"
            inputMode="url"
            placeholder="https://api.deepseek.com/v1"
            value={value("llm_base_url")}
            onChange={(e) => set("llm_base_url", e.target.value)}
          />
        </label>

        <label className="field">
          <span>
            {field("llm_api_key").label}
            <em className={`src src--${field("llm_api_key").source}`}>{sourceLabel(field("llm_api_key").source)}</em>
          </span>
          <input
            type="password"
            autoComplete="off"
            placeholder={field("llm_api_key").has_value ? `已保存 ${field("llm_api_key").masked}（留空则不修改）` : "sk-..."}
            value={value("llm_api_key")}
            onChange={(e) => set("llm_api_key", e.target.value)}
          />
          <small>留空 = 保持现有 key 不变；填新的 = 覆盖。</small>
        </label>

        <div className="settings__row">
          <label className="field">
            <span>{field("llm_model_planner").label}</span>
            <input
              list="models-planner"
              value={value("llm_model_planner")}
              onChange={(e) => set("llm_model_planner", e.target.value)}
            />
            {datalist("models-planner", activeProvider?.models ?? [])}
          </label>
          <label className="field">
            <span>{field("llm_model_fast").label}</span>
            <input
              list="models-fast"
              value={value("llm_model_fast")}
              onChange={(e) => set("llm_model_fast", e.target.value)}
            />
            {datalist("models-fast", activeProvider?.models ?? [])}
          </label>
          <label className="field">
            <span>{field("llm_model_vision").label}</span>
            <input
              list="models-vision"
              value={value("llm_model_vision")}
              onChange={(e) => set("llm_model_vision", e.target.value)}
            />
            {datalist("models-vision", activeProvider?.models ?? [])}
          </label>
        </div>

        <label className="field field--narrow">
          <span>{field("llm_mode").label}</span>
          <select value={value("llm_mode")} onChange={(e) => set("llm_mode", e.target.value)}>
            <option value="auto">自动（有 key 用模型，没 key 用规则）</option>
            <option value="real">始终用模型（失败直接报错）</option>
            <option value="mock">始终用规则（不调用模型，不产生费用）</option>
          </select>
        </label>
      </fieldset>

      <fieldset className="settings__group" disabled={busy}>
        <legend>生图（创意工单）</legend>

        <div className="settings__providers">
          {creativeProviders.map((p) => (
            <button
              key={p.id}
              type="button"
              className={`preset ${activeCreative?.id === p.id ? "preset--on" : ""}`}
              onClick={() => pickCreative(p)}
              title={p.note}
            >
              {p.label}
            </button>
          ))}
        </div>

        <label className="field">
          <span>
            {field("dashscope_api_key").label}
            <em className={`src src--${field("dashscope_api_key").source}`}>
              {sourceLabel(field("dashscope_api_key").source)}
            </em>
          </span>
          <input
            type="password"
            autoComplete="off"
            placeholder={
              field("dashscope_api_key").has_value
                ? `已保存 ${field("dashscope_api_key").masked}（留空则不修改）`
                : "sk-...（与 DeepSeek 的 key 不是同一家）"
            }
            value={value("dashscope_api_key")}
            onChange={(e) => set("dashscope_api_key", e.target.value)}
          />
        </label>

        <div className="settings__row">
          <label className="field">
            <span>{field("creative_image_model").label}</span>
            <input
              list="models-image"
              value={value("creative_image_model")}
              onChange={(e) => set("creative_image_model", e.target.value)}
            />
            {datalist("models-image", activeCreative?.models ?? [])}
          </label>
          <label className="field">
            <span>{field("creative_edit_model").label}</span>
            <input
              list="models-edit"
              value={value("creative_edit_model")}
              onChange={(e) => set("creative_edit_model", e.target.value)}
            />
            {datalist("models-edit", activeCreative?.models ?? [])}
          </label>
        </div>

        <label className="field">
          <span>{field("creative_adapter").label}</span>
          <input
            type="url"
            placeholder="留空 = 用本地生成；填 http://localhost:8000 = 转给外部创意服务"
            value={value("creative_adapter")}
            onChange={(e) => set("creative_adapter", e.target.value)}
          />
        </label>
      </fieldset>

      <div className="settings__actions">
        <button type="button" className="btn" onClick={() => void onSave()} disabled={busy}>
          保存
        </button>
        <button type="button" className="btn btn--ghost" onClick={() => void onTest("chat")} disabled={busy}>
          测试对话连接
        </button>
        <button type="button" className="btn btn--ghost" onClick={() => void onTest("creative")} disabled={busy}>
          测试生图 key
        </button>
        <button type="button" className="btn btn--danger" onClick={() => void onReset()} disabled={busy}>
          恢复 .env 默认
        </button>
      </div>

      {note && <p className={`note note--${note.tone}`}>{note.text}</p>}

      <PromoCalendar />

      <Accounts me={me} />

      <p className="hint settings__foot">
        「测试对话连接」只发一次 8 token 的请求；「测试生图 key」是一次文本调用，用来确认 key 有效，不会真的生成图片。
      </p>
    </section>
  );
}
