import { useId, useRef, useState } from "react";
import type { ChangeEvent, DragEvent } from "react";
import { FileChip } from "../components/FileChip";
import { Icon } from "../components/Icon";
import { formatKb } from "./validation";

type DropZoneProps = {
  /** Names the zone for assistive technology, for example "Job description file". */
  label: string;
  file: File | null;
  error: string | null;
  /** Read-only while analyzing: no remove button. */
  locked: boolean;
  onFile: (file: File) => void;
  onRemove: () => void;
};

/** A dashed drop zone with a "browse" button; once a file is chosen it becomes a FileChip row. */
export function DropZone({ label, file, error, locked, onFile, onRemove }: DropZoneProps) {
  const input = useRef<HTMLInputElement>(null);
  const errorId = useId();
  const [dragging, setDragging] = useState(false);

  if (file) {
    return (
      <div className="file-row">
        <FileChip name={file.name} meta={formatKb(file.size)} onRemove={locked ? undefined : onRemove} />
      </div>
    );
  }

  const choose = (event: ChangeEvent<HTMLInputElement>) => {
    const picked = event.target.files?.[0];
    event.target.value = ""; // so choosing the same file again still fires
    if (picked) onFile(picked);
  };
  const drop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    const dropped = event.dataTransfer.files[0];
    if (dropped) onFile(dropped);
  };

  return (
    <div>
      <div
        className="dropzone"
        data-invalid={error ? "true" : undefined}
        data-dragging={dragging ? "true" : undefined}
        onClick={() => input.current?.click()}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={drop}
      >
        <Icon name="upload" size={22} className="dropzone__icon" />
        <p className="dropzone__text t-body">
          Drop a PDF, Word or text file, or{" "}
          <button type="button" className="dropzone__browse" aria-describedby={error ? errorId : undefined}>
            browse<span className="sr-only"> for the {label.toLowerCase()}</span>
          </button>
        </p>
        <input
          ref={input}
          type="file"
          className="sr-only"
          tabIndex={-1}
          aria-label={label}
          accept=".pdf,.docx,.txt"
          onChange={choose}
          onClick={(event) => event.stopPropagation()}
        />
      </div>
      {error ? (
        <p id={errorId} className="field-error t-caption">
          <Icon name="alert" size={16} />
          {error}
        </p>
      ) : null}
    </div>
  );
}
