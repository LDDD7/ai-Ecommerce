/** 改密页：首次登录（must_change）或主动改密时使用。 */

import { useState } from "react";
import "../styles-login.css";
import { ApiError, changePassword, logout, type SessionUser } from "../api";

type Props = {
  user: SessionUser;
  /** 改完把最新用户回传（must_change 置 false） */
  onDone: (user: SessionUser) => void;
  onLogout: () => void;
  /** 是否是「被要求」改密（首次登录），影响文案与是否允许返回 */
  forced?: boolean;
};

export function ChangePassword({ user, onDone, onLogout, forced = false }: Props): JSX.Element {
  const [oldPw, setOldPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [again, setAgain] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    if (newPw.length < 6) {
      setError("新密码至少 6 位");
      return;
    }
    if (newPw !== again) {
      setError("两次输入的新密码不一致");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await changePassword(oldPw, newPw);
      onDone({ ...user, must_change: false });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "修改失败，请稍后再试");
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
            <h1 className="lg__title">{forced ? "先设置一个新密码" : "修改密码"}</h1>
            <p className="lg__sub">
              {user.name}（{user.username}）
              {forced ? " · 首次登录必须改密" : ""}
            </p>
          </div>
        </div>

        <form className="lg__form" onSubmit={submit}>
          <label className="lg__field">
            <span>原密码</span>
            <input
              type="password"
              value={oldPw}
              onChange={(e) => setOldPw(e.target.value)}
              autoComplete="current-password"
              autoFocus
            />
          </label>
          <label className="lg__field">
            <span>新密码（至少 6 位）</span>
            <input
              type="password"
              value={newPw}
              onChange={(e) => setNewPw(e.target.value)}
              autoComplete="new-password"
            />
          </label>
          <label className="lg__field">
            <span>再输一次</span>
            <input
              type="password"
              value={again}
              onChange={(e) => setAgain(e.target.value)}
              autoComplete="new-password"
            />
          </label>

          {error !== "" ? <p className="lg__error">{error}</p> : null}

          <button className="lg__submit" type="submit" disabled={busy || oldPw === "" || newPw === ""}>
            {busy ? "保存中…" : "保存新密码"}
          </button>
          <button className="lg__link" type="button" onClick={() => void logout().then(onLogout)}>
            退出登录
          </button>
        </form>
      </section>
    </main>
  );
}
