import { NavLink } from "react-router-dom";

const nav = [
  { to: "/", label: "Chat", icon: "💬" },
  { to: "/run", label: "Run", icon: "▶" },
  { to: "/eval", label: "Eval", icon: "📊" },
  { to: "/skills", label: "Skills", icon: "🧩" },
];

export default function Sidebar() {
  return (
    <aside className="w-56 shrink-0 border-r border-slate-800 bg-slate-900/60 flex flex-col">
      <div className="px-5 py-5 border-b border-slate-800">
        <div className="flex items-center gap-2">
          <div className="h-8 w-8 rounded-lg bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center text-white font-bold">
            D
          </div>
          <div>
            <div className="text-sm font-semibold text-slate-100">DevPilot</div>
            <div className="text-[10px] text-slate-500">AI Agent Studio</div>
          </div>
        </div>
      </div>
      <nav className="flex-1 px-2 py-3 space-y-1">
        {nav.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === "/"}
            className={({ isActive }) =>
              [
                "flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors",
                isActive
                  ? "bg-indigo-500/15 text-indigo-300 border border-indigo-500/30"
                  : "text-slate-400 hover:text-slate-200 hover:bg-slate-800/60 border border-transparent",
              ].join(" ")
            }
          >
            <span className="text-base">{item.icon}</span>
            <span>{item.label}</span>
          </NavLink>
        ))}
      </nav>
      <div className="px-4 py-3 text-[10px] text-slate-600 border-t border-slate-800">
        v0.1.0 · local
      </div>
    </aside>
  );
}
