import { useState, type FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../api";
import type { Role } from "../types";

export default function Users() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("user");
  const [done, setDone] = useState("");

  const create = useMutation({
    mutationFn: () => api.createUser(username.trim(), password, role),
    onSuccess: (u) => {
      setDone(`Created ${u.role} "${u.username}".`);
      setUsername("");
      setPassword("");
    },
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    setDone("");
    create.mutate();
  }

  return (
    <div className="page narrow">
      <h2>Create user</h2>
      <p className="muted">There is no self-signup. Give the new user their password through a secure channel.</p>
      <form className="card" onSubmit={submit}>
        <label>
          Username
          <input value={username} onChange={(e) => setUsername(e.target.value)} minLength={3} maxLength={64} required />
        </label>
        <label>
          Password (at least 10 characters)
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} minLength={10} required />
        </label>
        <label>
          Role
          <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
            <option value="user">User: search and chat</option>
            <option value="ingestor">Ingestor: user + upload documents</option>
            <option value="admin">Admin: everything, including delete and users</option>
          </select>
        </label>
        {create.error && <p className="error" role="alert">{(create.error as Error).message}</p>}
        {done && <p className="ok" role="status">{done}</p>}
        <button disabled={create.isPending}>{create.isPending ? "Creating…" : "Create user"}</button>
      </form>
    </div>
  );
}
