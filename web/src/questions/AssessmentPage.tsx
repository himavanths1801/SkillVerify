import { useNavigate, useParams } from "react-router-dom";
import { Button } from "../components/Button";
import { Card } from "../components/Card";

/** `/recruiter/assessment/:id`: a placeholder until the student screen exists. */
export function AssessmentPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  return (
    <Card className="placeholder">
      <h1 className="t-heading" tabIndex={-1}>Assessment created.</h1>
      <p className="t-body">The student screen comes in a later step.</p>
      <Button variant="secondary" onClick={() => navigate(`/recruiter/questions/${id}`)}>Back to question review</Button>
    </Card>
  );
}
