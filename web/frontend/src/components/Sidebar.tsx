import { NavLink } from "react-router-dom";
import { Sun, Moon } from "lucide-react";
import { getStoredTheme, setStoredTheme } from "../api";
import { useEffect, useState } from "react";

/** 56px SVG 图标导航 rail。 */
export default function Sidebar() {
  const [theme, setTheme] = useState<"dark" | "light">(() => {
    return getStoredTheme() ?? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  });

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "dark") {
      root.setAttribute("data-theme", "dark");
    } else {
      root.removeAttribute("data-theme");
      root.setAttribute("data-theme", "light");
    }
    setStoredTheme(theme);
  }, [theme]);

  function toggleTheme() {
    setTheme((t) => (t === "dark" ? "light" : "dark"));
  }

  return (
    <nav className="rail" aria-label="主导航">
      <div className="logo">D</div>

      <NavLink to="/" end className={({ isActive }) => isActive ? "on" : ""} aria-label="Chat" title="Chat">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 12a8 8 0 0 1-8 8H5l-2 2V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8Z" />
        </svg>
      </NavLink>

      <NavLink to="/run" className={({ isActive }) => isActive ? "on" : ""} aria-label="Run" title="Run">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="9" />
          <path d="m10 8 6 4-6 4V8Z" fill="currentColor" stroke="none" />
        </svg>
      </NavLink>

      <NavLink to="/eval" className={({ isActive }) => isActive ? "on" : ""} aria-label="Eval" title="Eval">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
          <path d="M4 20V10M10 20V4M16 20v-8M22 20H2" />
        </svg>
      </NavLink>

      <NavLink to="/skills" className={({ isActive }) => isActive ? "on" : ""} aria-label="Skills" title="Skills">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round">
          <path d="M10 3h4v4a2 2 0 0 0 4 0V6h3v7a2 2 0 0 1-2 2h-3v4h-4v-3a2 2 0 0 0-4 0v3H7v-4H4a2 2 0 0 1-2-2V6h4v1a2 2 0 0 0 4 0V3Z" />
        </svg>
      </NavLink>

      <div className="spacer" />

      <button
        className="rail-btn"
        onClick={toggleTheme}
        aria-label={theme === "dark" ? "切换浅色" : "切换深色"}
        title={theme === "dark" ? "切换浅色" : "切换深色"}
      >
        {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
      </button>
    </nav>
  );
}