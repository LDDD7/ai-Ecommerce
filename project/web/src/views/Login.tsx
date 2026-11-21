/** 登录页：未登录时整站只显示这一个页面。 */

import { useState } from "react";
import "../styles-login.css";
import { ApiError, login, type SessionUser } from "../api";

type Props = {
  onDone: (user: SessionUser) => void;
  /** 会话过期被踢回登录页时，给一句解释 */
  notice?: string;
};

export function Login({ onDone, notice }: Props): JSX.Element {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const res = await login(username.trim(), password);
      onDone(res.user);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "登录失败，请稍后再试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="lg">
      <section className="lg__card">
        <div className="lg__brand">
          <span className="lg__mark">诊</span>
          <div>
            <h1 className="lg__title">店诊 StoreDoctor</h1>
            <p className="lg__sub">店铺经营诊断 · 归因 · 动作闭环</p>
          </div>
        </div>

        {notice ? <p className="lg__notice">{notice}</p> : null}

        <form className="lg__form" onSubmit={submit}>
          <label className="lg__field">
            <span>用户名</span>
            <input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              autoFocus
              placeholder="admin"
              spellCheck={false}
            />
          </label>
          <label className="lg__field">
            <span>密码</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              placeholder="••••••"
            />
          </label>

          {error !== "" ? <p className="lg__error">{error}</p> : null}

          <button className="lg__submit" type="submit" disabled={busy || username.trim() === "" || password === ""}>
            {busy ? "登录中…" : "登录"}
          </button>
        </form>

        <p className="lg__foot">首次使用请见项目根目录的 .tmp/first-run.txt，或联系店主开账号。</p>
      </section>
    </main>
  );
}
