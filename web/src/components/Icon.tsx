import type { ReactElement, SVGProps } from "react";

/** The few line icons the base components need. Drawn with currentColor, so a parent sets the colour. */
export type IconName = "check" | "eye" | "dash" | "circle" | "clock" | "alert" | "file" | "x" | "chevron-down" | "check-circle" | "upload" | "info" | "branch" | "pencil" | "repo";

const PATHS: Record<IconName, ReactElement> = {
  check: <path d="M4 10.5l4 4 8-9" />,
  "check-circle": (
    <>
      <circle cx="10" cy="10" r="7.25" />
      <path d="M6.5 10.5l2.5 2.5 4.5-5.5" />
    </>
  ),
  eye: (
    <>
      <path d="M1.75 10S5 4.5 10 4.5 18.25 10 18.25 10 15 15.5 10 15.5 1.75 10 1.75 10z" />
      <circle cx="10" cy="10" r="2.5" />
    </>
  ),
  dash: (
    <>
      <circle cx="10" cy="10" r="7.25" />
      <path d="M6.75 10h6.5" />
    </>
  ),
  circle: <circle cx="10" cy="10" r="7.25" />,
  clock: (
    <>
      <circle cx="10" cy="10" r="7.25" />
      <path d="M10 5.75V10l2.75 1.75" />
    </>
  ),
  alert: (
    <>
      <path d="M10 2.75l8 14H2l8-14z" />
      <path d="M10 8v4" />
      <path d="M10 14.4v.1" />
    </>
  ),
  file: (
    <>
      <path d="M5 2.75h6.5L15.25 6.5v10.75H5z" />
      <path d="M11.5 2.75V6.5h3.75" />
    </>
  ),
  x: <path d="M5.5 5.5l9 9M14.5 5.5l-9 9" />,
  "chevron-down": <path d="M5 7.75l5 5 5-5" />,
  info: (
    <>
      <circle cx="10" cy="10" r="7.25" />
      <path d="M10 9.25v4.25" />
      <path d="M10 6.5v.1" />
    </>
  ),
  branch: (
    <>
      <circle cx="5.5" cy="4.5" r="1.75" />
      <circle cx="5.5" cy="15.5" r="1.75" />
      <circle cx="14.5" cy="7.5" r="1.75" />
      <path d="M5.5 6.25v7.5M14.5 9.25c0 2.75-3.25 3.25-9 4.25" />
    </>
  ),
  pencil: <path d="M3.5 16.5l.75-3.5L13.5 3.75l2.75 2.75L7 15.75zM11.75 5.5l2.75 2.75" />,
  repo: (
    <>
      <path d="M4.5 3.5h9.75a1.25 1.25 0 011.25 1.25v10.5H5.75A1.25 1.25 0 004.5 16.5z" />
      <path d="M4.5 16.5V3.5M7.5 6.5h5" />
    </>
  ),
  upload: <path d="M10 13V3.75M6.25 7.25L10 3.5l3.75 3.75M3.5 12.5v3.75h13V12.5" />,
};

type IconProps = Omit<SVGProps<SVGSVGElement>, "name"> & { name: IconName; size?: number };

/** Decorative by default (aria-hidden): the text or the aria-label of the control carries the meaning. */
export function Icon({ name, size = 18, ...rest }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      data-icon={name}
      {...rest}
    >
      {PATHS[name]}
    </svg>
  );
}
