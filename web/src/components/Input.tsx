import { useId } from "react";
import type { InputHTMLAttributes } from "react";

type InputProps = InputHTMLAttributes<HTMLInputElement> & {
  label: string;
  helper?: string;
  /** Shows the focused look in the design-system preview. */
  forceFocus?: boolean;
};

export function Input({ label, helper, forceFocus, id, className = "", ...rest }: InputProps) {
  const generated = useId();
  const inputId = id ?? generated;
  const helperId = helper ? `${inputId}-helper` : undefined;
  return (
    <div className="sv-field">
      <label className="sv-field__label" htmlFor={inputId}>
        {label}
      </label>
      <input
        id={inputId}
        className={`sv-input ${className}`.trim()}
        aria-describedby={helperId}
        data-force={forceFocus ? "focus" : undefined}
        {...rest}
      />
      {helper ? (
        <span id={helperId} className="sv-field__helper">
          {helper}
        </span>
      ) : null}
    </div>
  );
}
