import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  getDiagnose,
  getHealth,
  getMe,
  getOverview,
  logout,
  roleRank,
  ROLE_LABEL,
  type Diagnose,
  type Health,
  type Overview,
  type SessionUser,
} from "./api";
import { compactMoney, money } from "./format";
import ActionDetail from "./views/ActionDetail";
import Approval from "./views/Approval";
import Chat from "./views/Chat";
import { ChangePassword } from "./views/ChangePassword";
import Dashboard from "./views/Dashboard";
import DataImport from "./views/DataImport";
import Diagnosis from "./views/Diagnosis";
import { Login } from "./views/Login";
import Query from "./views/Query";
import Settings from "./views/Settings";
import { Trace } from "./views/Trace";

type Tab = "dashboard" | "chat" | "diagnosis" | "query" | "actions" | "import" | "trace" | "settings";
type State =
  | { phase: "loading" }
  | { phase: "ready"; health: Health; overview: Overview; diagnose: Diagnose }
  | { phase: "error"; message: string; status?: number };

/** 只有店主能进的功能（与后端 require_role("owner") 对齐） */
const OWNER_TABS: Tab[] = ["settings", "import"];

export default function App() {
  const [session, setSession] = useState<"checking" | "anon" | "ok">("checking");
  const [user, setUser] = useState<SessionUser | null>(null);
  const [notice, setNotice] = useState("");
  const [state, setState] = useState<State>({ phase: "loading" });
  const [tab, setTab] = useState<Tab>("dashboard");
  const [openAction, setOpenAction] = useState<string | null>(null);
  const [actionsKey, setActionsKey] = useState(0);
  const [traceId, setTraceId] = useState<string | null>(null);
  const [changingPw, setChangingPw] = useState(false);

  const load = useCallback(async () => {
    setState({ phase: "loading" });
    try {
      const health = await getHealth();
      const [overview, diagnose] = await Promise.all([getOverview(7), getDiagnose()]);
      setState({ phase: "ready", health, overview, diagnose });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setState({ phase: "error", message, status: err instanceof ApiError ? err.status : undefined });
    }
  }, []);

  // 启动先问「我是谁」：登录态由服务端 Cookie 决定，前端不存 token
  useEffect(() => {
    let alive = true;
    getMe()
      .then((res) => {
        if (!alive) return;
        setUser(res.user);
        setSession("ok");
      })
      .catch(() => {
        if (alive) setSession("anon");
      });
    return () => {
      alive = false;
    };
  }, []);

  // 任何接口 401 都会广播，回到登录页并说清楚原因
  useEffect(() => {
    const onUnauthorized = () => {
      setUser(null);
      setSession("anon");
      setOpenAction(null);
      setNotice("登录状态已失效，请重新登录。");
    };
    window.addEventListener("sd:unauthorized", onUnauthorized);
    return () => window.removeEventListener("sd:unauthorized", onUnauthorized);
  }, []);

  useEffect(() => {
    if (session === "ok" && user) void load();
  }, [session, user, load]);

  // 换账号后，如果当前页不在新角色权限内，退回体检台（否则会看到 403 空页）
  useEffect(() => {
    if (!user) return;
    if (OWNER_TABS.includes(tab) && roleRank(user.role) < 3) setTab("dashboard");
  }, [user, tab]);

  const doLogout = useCallback(async () => {
    try {
      await logout();
    } catch {
      /* 退出失败也让前端回到登录页，Cookie 过期与否由服务端判断 */
    }
    setUser(null);
    setSession("anon");
    setOpenAction(null);
    setNotice("");
  }, []);

  if (session === "checking") {
    return <p className="hint">正在检查登录状态…</p>;
  }

  if (session === "anon" || !user) {
    return (
      <Login
        notice={notice}
        onDone={(u: SessionUser) => {
          setUser(u);
          setSession("ok");
          setNotice("");
        }}
      />
    );
  }

  if (user.must_change) {
    return (
      <ChangePassword
        user={user}
        forced
        onDone={(u: SessionUser) => setUser(u)}
        onLogout={() => void doLogout()}
      />
    );
  }

  // 主动改密：和「首次登录必须改密」用同一个页面，只是不强制
  if (changingPw) {
    return (
      <ChangePassword
        user={user}
        onDone={(u: SessionUser) => {
          setUser(u);
          setChangingPw(false);
        }}
        onLogout={() => void doLogout()}
      />
    );
  }

  const isOwner = roleRank(user.role) >= 3;
  const canApprove = roleRank(user.role) >= 2;

  return (
    <main className="sheet">
      <header className="sheet__head">
        <div>
          <p className="eyebrow">StoreDoctor · 店诊</p>
          <h1>店铺经营诊断</h1>
          <p className="sub">
            {state.phase === "ready"
              ? `诊断日 ${state.overview.as_of} · 对比 ${state.overview.window.prev_start}~${state.overview.window.prev_end} · ${state.health.metrics_count} 个指标 · ${
                  state.health.llm.configured ? "LLM 已配置" : "规则版（未配 key）"
                } · 健康分 ${state.overview.health.grade} 级`
              : "正在读取数据…"}
          </p>
        </div>
        <div
          className={`seal ${state.phase === "ready" && state.overview.health.score >= 70 ? "seal--ok" : "seal--warn"}`}
          aria-label={state.phase === "ready" ? `健康分 ${state.overview.health.score}，${state.overview.health.grade} 级` : "健康分加载中"}
          title="健康分"
        >
          {state.phase === "ready" ? state.overview.health.score : "…"}
        </div>
      </header>

      {state.phase === "ready" ? (
        <>
          <nav className="tabs" aria-label="视图切换">
            <button
              type="button"
              className={tab === "dashboard" ? "on" : ""}
              aria-current={tab === "dashboard" ? "page" : undefined}
              onClick={() => setTab("dashboard")}
            >
              体检台
            </button>
            <button
              type="button"
              className={tab === "diagnosis" ? "on" : ""}
              aria-current={tab === "diagnosis" ? "page" : undefined}
              onClick={() => setTab("diagnosis")}
            >
              诊断与证据
            </button>
            <button
              type="button"
              className={tab === "chat" ? "on" : ""}
              aria-current={tab === "chat" ? "page" : undefined}
              onClick={() => setTab("chat")}
            >
              问诊
            </button>
            <button
              type="button"
              className={tab === "query" ? "on" : ""}
              aria-current={tab === "query" ? "page" : undefined}
              onClick={() => setTab("query")}
            >
              查数
            </button>
            <button
              type="button"
              className={tab === "actions" ? "on" : ""}
              aria-current={tab === "actions" ? "page" : undefined}
              onClick={() => setTab("actions")}
            >
              执行
            </button>
            {isOwner ? (
              <button
                type="button"
                className={tab === "import" ? "on" : ""}
                aria-current={tab === "import" ? "page" : undefined}
                onClick={() => setTab("import")}
              >
                数据导入
              </button>
            ) : null}
            <button
              type="button"
              className={tab === "trace" ? "on" : ""}
              aria-current={tab === "trace" ? "page" : undefined}
              onClick={() => setTab("trace")}
            >
              链路
            </button>
            {isOwner ? (
              <button
                type="button"
                className={tab === "settings" ? "on" : ""}
                aria-current={tab === "settings" ? "page" : undefined}
                onClick={() => setTab("settings")}
              >
                设置
              </button>
            ) : null}
            <span className="tabs__meta">
              成交额 {compactMoney(state.diagnose.gmv.total_cur)} · 影响 {money(state.diagnose.cards.reduce((s, c) => s + Math.abs(c.gmv_impact ?? 0), 0))}
            </span>
            <span className="tabs__user" title={`${user.display_name}（${user.username}）`}>
              <b>{user.name}</b>
              <span className="tabs__role">{ROLE_LABEL[user.role] ?? user.role}</span>
              <button type="button" className="tabs__logout" onClick={() => setChangingPw(true)}>
                改密
              </button>
              <button type="button" className="tabs__logout" onClick={() => void doLogout()}>
                退出
              </button>
            </span>
          </nav>

          {tab === "dashboard" ? (
            <Dashboard overview={state.overview} diagnose={state.diagnose} onOpenDiagnosis={() => setTab("diagnosis")} />
          ) : tab === "chat" ? (
            <Chat asOf={state.overview.as_of} onOpenDiagnosis={() => setTab("diagnosis")} />
          ) : tab === "query" ? (
            <Query asOf={state.overview.as_of} />
          ) : tab === "actions" ? (
            <>
              <p className="hint">
                Agent 起草的动作先落到这里：低风险可直接执行，其余需要审批；执行后可以回滚，每一步都留审计。
              </p>
              <Approval onOpen={setOpenAction} refreshKey={actionsKey} />
              {openAction ? (
                <ActionDetail
                  actionId={openAction}
                  myRole={user.role}
                  canApprove={canApprove}
                  onClose={() => setOpenAction(null)}
                  onChanged={() => setActionsKey((k) => k + 1)}
                  onOpenTrace={(id) => {
                    setTraceId(id);
                    setOpenAction(null);
                    setTab("trace");
                  }}
                />
              ) : null}
            </>
          ) : tab === "trace" ? (
            <Trace initialTraceId={traceId} />
          ) : tab === "settings" ? (
            <Settings me={user} />
          ) : tab === "import" ? (
            <DataImport />
          ) : (
            <Diagnosis
              diagnose={state.diagnose}
              onApplied={(actionId) => {
                setActionsKey((k) => k + 1);
                setOpenAction(actionId);
                setTab("actions");
              }}
            />
          )}
        </>
      ) : null}

      {state.phase === "loading" ? <p className="hint">加载中…</p> : null}

      {state.phase === "error" ? (
        <section className="panel">
          <h2>后端没连上</h2>
          <p className="note">
            {state.message}
            {state.status ? `（HTTP ${state.status}）` : ""}
          </p>
          <pre className="cmd">{`cd project
.venv\\Scripts\\python -m uvicorn app.main:app --reload --port 8000`}</pre>
          <button type="button" className="link" onClick={() => void load()}>
            重试
          </button>
        </section>
      ) : null}
    </main>
  );
}
