import type { HTMLAttributes } from "react";

/** A surface with the mode's card radius, padding and shadow. Cards have no border. */
export function Card({ className = "", ...rest }: HTMLAttributes<HTMLDivElement>) {
  return <div className={`sv-card ${className}`.trim()} {...rest} />;
}
