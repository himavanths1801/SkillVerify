type ToggleProps = {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  disabled?: boolean;
};

/** An on/off switch. It is a real button, so Space and Enter toggle it from the keyboard. */
export function Toggle({ checked, onChange, label, disabled }: ToggleProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      className="sv-toggle"
      onClick={() => onChange(!checked)}
    >
      <span className="sv-toggle__track" aria-hidden="true">
        <span className="sv-toggle__knob" />
      </span>
      <span className="sv-toggle__label">{label}</span>
    </button>
  );
}
