import type { ReactNode } from "react";

/** `inline code` between backticks becomes a <code>; everything else stays literal text (never HTML). */
function inline(text: string, keyPrefix: string): ReactNode[] {
  const parts: ReactNode[] = [];
  const pattern = /`([^`\n]+)`/g;
  let last = 0;
  let index = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > last) parts.push(text.slice(last, match.index));
    parts.push(<code key={`${keyPrefix}-c${index++}`} className="rt-code">{match[1]}</code>);
    last = match.index + match[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

const NUMBERED = /^\s*(\d+)\.\s+(.*)$/;

/**
 * A small safe renderer for a question: paragraphs split on blank lines, line breaks kept, `inline code`, and lines
 * that start with "1." "2." as an ordered list. Only React elements are made, so HTML in the text stays text.
 */
export function RichText({ text, className = "" }: { text: string; className?: string }) {
  const blocks = text.replace(/\r\n?/g, "\n").trim().split(/\n\s*\n/);
  const out: ReactNode[] = [];
  blocks.forEach((block, b) => {
    let lines: string[] = [];
    let items: { n: number; text: string }[] = [];
    const flushLines = () => {
      if (lines.length === 0) return;
      const key = `p${b}-${out.length}`;
      out.push(
        <p key={key} className="rt-p">
          {lines.map((line, i) => (
            <span key={i}>
              {i > 0 ? <br /> : null}
              {inline(line, `${key}-${i}`)}
            </span>
          ))}
        </p>,
      );
      lines = [];
    };
    const flushItems = () => {
      if (items.length === 0) return;
      const key = `l${b}-${out.length}`;
      out.push(
        <ol key={key} className="rt-ol" start={items[0].n !== 1 ? items[0].n : undefined}>
          {items.map((item, i) => <li key={i}>{inline(item.text, `${key}-${i}`)}</li>)}
        </ol>,
      );
      items = [];
    };
    for (const line of block.split("\n")) {
      const match = NUMBERED.exec(line);
      if (match) {
        flushLines();
        items.push({ n: Number(match[1]), text: match[2] });
      } else {
        flushItems();
        lines.push(line);
      }
    }
    flushLines();
    flushItems();
  });
  return <div className={`rt ${className}`.trim()}>{out}</div>;
}
