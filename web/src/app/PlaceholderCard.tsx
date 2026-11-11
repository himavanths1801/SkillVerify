import type { ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "../components/Button";
import { Card } from "../components/Card";

type Props = { title: string; description: string; next: string; children?: ReactNode };

/** One Card: heading, one sentence, optional extra content, and a primary Continue button. */
export function PlaceholderCard({ title, description, next, children }: Props) {
  const navigate = useNavigate();
  return (
    <Card className="placeholder">
      <h1 className="t-heading" tabIndex={-1}>{title}</h1>
      <p className="t-body">{description}</p>
      {children}
      <div><Button onClick={() => navigate(next)}>Continue</Button></div>
    </Card>
  );
}
