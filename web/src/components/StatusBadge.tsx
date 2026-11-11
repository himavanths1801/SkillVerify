import type { ReactNode } from "react";
import { Icon } from "./Icon";
import type { IconName } from "./Icon";

export type BadgeVariant = "found" | "gold" | "neutral" | "ready" | "notstarted";

/** Every badge has an icon AND text, so meaning never depends on colour alone. */
const BADGES: Record<BadgeVariant, { icon: IconName; text: string }> = {
  found: { icon: "check", text: "Found" },
  gold: { icon: "eye", text: "Needs a look" },
  neutral: { icon: "dash", text: "Not checked" },
  ready: { icon: "check-circle", text: "Ready" },
  notstarted: { icon: "circle", text: "Not started" },
};

type StatusBadgeProps = { variant: BadgeVariant; children?: ReactNode; /** Overrides the variant's default icon. */ icon?: IconName };

export function StatusBadge({ variant, children, icon: iconOverride }: StatusBadgeProps) {
  const { icon, text } = BADGES[variant];
  return (
    <span className={`sv-badge sv-badge--${variant}`}>
      <Icon name={iconOverride ?? icon} size={16} className="sv-badge__icon" />
      <span className="sv-badge__text">{children ?? text}</span>
    </span>
  );
}
