import { useEffect, useState } from "react";
import { getSkills, type Skill } from "../api";

export default function SkillsPage() {
  const [skills, setSkills] = useState<Skill[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<Record<string, boolean>>({});

  useEffect(() => {
    getSkills()
      .then(setSkills)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, []);

  function toggle(name: string) {
    setOpen((p) => ({ ...p, [name]: !p[name] }));
  }

  return (
    <div className="h-full flex flex-col">
      <header className="px-6 py-4 border-b border-[var(--line)]">
        <h1 className="text-lg font-semibold">Skills</h1>
        <p className="text-xs text-[var(--text3)]">Registered skills and their tool specs.</p>
      </header>
      <div className="flex-1 overflow-auto px-6 py-6">
        {error && (
          <div className="px-3 py-2 rounded-md bg-rose-500/10 border border-rose-500/30 text-rose-300 text-sm">
            {error}
          </div>
        )}
        {!skills && !error && (
          <div className="text-[var(--text3)] text-sm">Loading skills…</div>
        )}
        {skills && skills.length === 0 && (
          <div className="text-[var(--text3)] text-sm">No skills registered.</div>
        )}
        <div className="grid gap-3">
          {skills?.map((skill) => {
            const isOpen = !!open[skill.name];
            return (
              <div
                key={skill.name}
                className="rounded-lg border border-[var(--line)] bg-[var(--sunken)] overflow-hidden"
              >
                <button
                  onClick={() => toggle(skill.name)}
                  className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-[var(--panel)]"
                >
                  <div className="flex items-center gap-3">
                    <span className="h-7 w-7 rounded-md bg-indigo-500/15 text-indigo-300 flex items-center justify-center text-xs font-mono">
                      {skill.name.slice(0, 2)}
                    </span>
                    <div>
                      <div className="text-sm font-medium text-[var(--text)]">{skill.name}</div>
                      <div className="text-[11px] text-[var(--text3)]">
                        {skill.specs.length} spec{skill.specs.length === 1 ? "" : "s"}
                      </div>
                    </div>
                  </div>
                  <span className={`text-[var(--text3)] transition-transform ${isOpen ? "rotate-90" : ""}`}>
                    ▶
                  </span>
                </button>
                {isOpen && (
                  <div className="px-4 pb-4 pt-1 space-y-3 border-t border-[var(--line)]">
                    {skill.specs.map((spec) => (
                      <div key={spec.name} className="rounded-md bg-[var(--ground)] border border-[var(--line)] p-3">
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-sm text-indigo-300">{spec.name}</span>
                        </div>
                        <p className="text-xs text-[var(--text2)] mt-1">{spec.description}</p>
                        <div className="mt-2">
                          <div className="text-[10px] uppercase tracking-wider text-[var(--text3)] mb-1">
                            Schema
                          </div>
                          <pre className="text-[11px] text-[var(--text2)] overflow-auto bg-[var(--sunken)] rounded p-2">
                            {JSON.stringify(spec.schema, null, 2)}
                          </pre>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
