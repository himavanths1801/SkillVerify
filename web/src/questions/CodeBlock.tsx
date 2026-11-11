import { useId, useState } from "react";
import { languageLabel, PREVIEW_LINES } from "./format";

/** The starter code: mono font, no ligatures, sideways scroll. `collapsible` shows the first 8 lines until asked. */
export function CodeBlock({ code, language, collapsible = false }: { code: string; language: string; collapsible?: boolean }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const lines = code.replace(/\s+$/, "").split("\n");
  const long = collapsible && lines.length > PREVIEW_LINES;
  const shown = long && !open ? lines.slice(0, PREVIEW_LINES) : lines;
  return (
    <div className="code">
      <div className="code__head">
        <span className="t-label">Starter code</span>
        <span className="code__lang t-caption">{languageLabel(language)}</span>
      </div>
      <pre className="code__box t-code" id={id} tabIndex={0} aria-label="Starter code"><code>{shown.join("\n")}</code></pre>
      {long ? (
        <button type="button" className="text-btn t-caption" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>
          {open ? "Show fewer" : `Show all ${lines.length} lines`}
        </button>
      ) : null}
    </div>
  );
}
