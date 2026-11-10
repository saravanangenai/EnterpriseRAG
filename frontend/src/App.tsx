import { NavLink, Navigate, Outlet, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth";
import Chat from "./pages/Chat";
import Documents from "./pages/Documents";
import Login from "./pages/Login";
import Users from "./pages/Users";

function Layout() {
  const { user, loading, logout } = useAuth();
  if (loading) return <p className="center muted">Loading…</p>;
  if (!user) return <Navigate to="/login" replace />;
  return (
    <div className="shell">
      <header className="topbar">
        <strong>Enterprise RAG</strong>
        <nav>
          <NavLink to="/">Chat</NavLink>
          <NavLink to="/documents">Documents</NavLink>
          {user.role === "admin" && <NavLink to="/users">Users</NavLink>}
        </nav>
        <span className="spacer" />
        <span className="muted">
          {user.username} · {user.role}
        </span>
        <button className="ghost" onClick={logout}>
          Sign out
        </button>
      </header>
      <main>
        <Outlet />
      </main>
    </div>
  );
}

function AdminOnly({ children }: { children: JSX.Element }) {
  const { user } = useAuth();
  return user?.role === "admin" ? children : <Navigate to="/" replace />;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<Layout />}>
        <Route index element={<Chat />} />
        <Route path="documents" element={<Documents />} />
        <Route
          path="users"
          element={
            <AdminOnly>
              <Users />
            </AdminOnly>
          }
        />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
