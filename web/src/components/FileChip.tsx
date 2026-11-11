import { Icon } from "./Icon";

type FileChipProps = {
  name: string;
  /** When given, a remove button appears. */
  onRemove?: () => void;
  /** Optional text after the name, pushed to the right (for example the file size). */
  meta?: string;
  className?: string;
};

/** A file name in the mono font, with a file icon and an optional remove button. */
export function FileChip({ name, onRemove, meta, className = "" }: FileChipProps) {
  return (
    <span className={`sv-chip t-code ${className}`.trim()}>
      <Icon name="file" size={16} />
      <span className="sv-chip__name">{name}</span>
      {meta ? <span className="sv-chip__meta">{meta}</span> : null}
      {onRemove ? (
        <button type="button" className="sv-chip__remove" aria-label={`Remove ${name}`} onClick={onRemove}>
          <Icon name="x" size={14} />
        </button>
      ) : null}
    </span>
  );
}
