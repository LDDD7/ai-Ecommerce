/** 账号与权限：只有店主能进来（App 里已按角色隐藏入口，后端也 require_role("owner")）。 */

import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  ROLE_LABEL,
  createUser,
  listUsers,
  patchUser,
  type Role,
  type SessionUser,
  type UserRow,
} from "../api";
import { fmtTime } from "../audit-labels";

type Props = { me: SessionUser };

const ROLES: Role[] = ["staff", "manager", "owner"];

const ROLE_HINT: Record<Role, string> = {
  staff: "查数、看诊断、让 Agent 起草案",
  manager: "上一档 + 审批、执行、驳回、回滚",
  owner: "上一档 + 设置、导入数据、管账号",
};

function errorText(err: unknown): string {
  return err instanceof ApiError ? err.message : err instanceof Error ? err.message : String(err);
}

export function Accounts({ me }: Props): JSX.Element {
  const [rows, setRows] = useState<UserRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);

  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("staff");

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const res = await listUsers();
      setRows(res.items);
    } catch (err) {
      setNote({ tone: "bad", text: errorText(err) });
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    if (username.trim() === "" || password.length < 6) {
      setNote({ tone: "bad", text: "用户名不能为空，密码至少 6 位" });
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      await createUser({
        username: username.trim(),
        password,
        role,
        display_name: displayName.trim() || undefined,
      });
      setUsername("");
      setDisplayName("");
      setPassword("");
      setNote({ tone: "ok", text: "账号已创建，对方首次登录会被要求改密。" });
      await load();
    } catch (err) {
      setNote({ tone: "bad", text: errorText(err) });
    } finally {
      setBusy(false);
    }
  };

  const changeRole = async (row: UserRow, next: Role) => {
    setBusy(true);
    try {
      await patchUser(row.user_id, { role: next });
      await load();
    } catch (err) {
      setNote({ tone: "bad", text: errorText(err) });
    } finally {
      setBusy(false);
    }
  };

  const toggleDisabled = async (row: UserRow) => {
    const verb = row.disabled ? "恢复" : "停用";
    if (!window.confirm(`${verb}账号「${row.name}」？${row.disabled ? "" : "停用后无法登录，已登录的会被踢下线。"}`)) return;
    setBusy(true);
    try {
      await patchUser(row.user_id, { disabled: !row.disabled });
      setNote({ tone: "ok", text: `已${verb}「${row.name}」` });
      await load();
    } catch (err) {
      setNote({ tone: "bad", text: errorText(err) });
    } finally {
      setBusy(false);
    }
  };

  const resetPassword = async (row: UserRow) => {
    const next = window.prompt(`给「${row.name}」设置新密码（至少 6 位）。对方下次登录会被要求再改一次。`);
    if (next === null) return;
    if (next.length < 6) {
      setNote({ tone: "bad", text: "密码至少 6 位" });
      return;
    }
    setBusy(true);
    try {
      await patchUser(row.user_id, { password: next });
      setNote({ tone: "ok", text: `已重置「${row.name}」的密码` });
    } catch (err) {
      setNote({ tone: "bad", text: errorText(err) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <fieldset className="settings__group" disabled={busy}>
      <legend>账号与权限</legend>

      <table className="acc">
        <thead>
          <tr>
            <th scope="col">账号</th>
            <th scope="col">角色</th>
            <th scope="col">最近登录</th>
            <th scope="col">状态</th>
            <th scope="col">操作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const self = row.user_id === me.user_id;
            return (
              <tr key={row.user_id} className={row.disabled ? "acc__row--off" : ""}>
                <td>
                  <b>{row.name}</b>
                  {self ? <span className="acc__self">你</span> : null}
                  <span className="acc__login">{row.username}</span>
                </td>
                <td>
                  <select
                    value={row.role}
                    disabled={busy || self}
                    aria-label={`${row.name} 的角色`}
                    onChange={(event) => void changeRole(row, event.currentTarget.value as Role)}
                  >
                    {ROLES.map((r) => (
                      <option key={r} value={r}>
                        {ROLE_LABEL[r]}
                      </option>
                    ))}
                  </select>
                </td>
                <td className="acc__time">{row.last_login_at === null ? "从未" : fmtTime(row.last_login_at)}</td>
                <td>{row.disabled ? <span className="chip chip--low">已停用</span> : "正常"}</td>
                <td className="acc__ops">
                  <button type="button" className="acc__btn" disabled={busy} onClick={() => void resetPassword(row)}>
                    重置密码
                  </button>
                  <button
                    type="button"
                    className="acc__btn"
                    disabled={busy || self}
                    onClick={() => void toggleDisabled(row)}
                  >
                    {row.disabled ? "恢复" : "停用"}
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <form className="acc__add" onSubmit={add}>
        <label className="field">
          <span>用户名</span>
          <input
            value={username}
            disabled={busy}
            spellCheck={false}
            placeholder="lisi"
            onChange={(e) => setUsername(e.target.value)}
          />
        </label>
        <label className="field">
          <span>显示名</span>
          <input
            value={displayName}
            disabled={busy}
            placeholder="李四（可留空，默认用用户名）"
            onChange={(e) => setDisplayName(e.target.value)}
          />
        </label>
        <label className="field">
          <span>初始密码</span>
          <input
            type="password"
            value={password}
            disabled={busy}
            autoComplete="new-password"
            placeholder="至少 6 位"
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <label className="field field--narrow">
          <span>角色</span>
          <select value={role} disabled={busy} onChange={(e) => setRole(e.currentTarget.value as Role)}>
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {ROLE_LABEL[r]} · {ROLE_HINT[r]}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" className="btn" disabled={busy}>
          新增账号
        </button>
      </form>

      {note && <p className={`note note--${note.tone === "ok" ? "flat" : "bad"}`}>{note.text}</p>}

      <p className="hint">
        密码以 scrypt 哈希存库；登录态是 HttpOnly Cookie，页面拿不到、也存不了 token。
        员工离职先「停用」，历史审批仍保留原操作人。
      </p>
    </fieldset>
  );
}
