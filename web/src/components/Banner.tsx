import { Button } from "./Button";
import { Icon } from "./Icon";

type BannerProps = {
  title: string;
  message?: string;
  /** When given, a Retry button appears. */
  onRetry?: () => void;
};

/** An error banner: rose/err wash, err-accent border, alert icon, title, message and an optional Retry. */
export function Banner({ title, message, onRetry }: BannerProps) {
  return (
    <div className="sv-banner" role="alert">
      <Icon name="alert" size={22} className="sv-banner__icon" />
      <div className="sv-banner__body">
        <div className="sv-banner__title">{title}</div>
        {message ? <div className="sv-banner__message">{message}</div> : null}
      </div>
      {onRetry ? (
        <Button variant="secondary" onClick={onRetry}>
          Retry
        </Button>
      ) : null}
    </div>
  );
}
