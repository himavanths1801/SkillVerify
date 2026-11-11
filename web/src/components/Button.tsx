import type { ComponentProps } from "react";

export type ButtonVariant = "primary" | "secondary";
/** Lets the design-system preview show hover / pressed / focus-visible without a real pointer. */
export type ForcedState = "hover" | "pressed" | "focus";

type ButtonProps = ComponentProps<"button"> & {
  variant?: ButtonVariant;
  forceState?: ForcedState;
};

export function Button({ variant = "primary", forceState, className = "", type = "button", ...rest }: ButtonProps) {
  return (
    <button
      type={type}
      className={`sv-btn sv-btn--${variant} ${className}`.trim()}
      data-force={forceState}
      {...rest}
    />
  );
}
