import { useId } from "react";
import type { SelectHTMLAttributes } from "react";
import { Icon } from "./Icon";

type SelectProps = SelectHTMLAttributes<HTMLSelectElement> & {
  label: string;
  helper?: string;
  forceFocus?: boolean;
};

/** A native select (so keyboard and screen readers just work) with the same border rules as Input. */
export function Select({ label, helper, forceFocus, id, className = "", children, ...rest }: SelectProps) {
  const generated = useId();
  const selectId = id ?? generated;
  const helperId = helper ? `${selectId}-helper` : undefined;
  return (
    <div className="sv-field">
      <label className="sv-field__label" htmlFor={selectId}>
        {label}
      </label>
      <div className="sv-select-wrap">
        <select
          id={selectId}
          className={`sv-select ${className}`.trim()}
          aria-describedby={helperId}
          data-force={forceFocus ? "focus" : undefined}
          {...rest}
        >
          {children}
        </select>
        <Icon name="chevron-down" className="sv-select__chevron" />
      </div>
      {helper ? (
        <span id={helperId} className="sv-field__helper">
          {helper}
        </span>
      ) : null}
    </div>
  );
}
