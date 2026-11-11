import { Navigate, Route, Routes } from "react-router-dom";
import { RecruiterLayout } from "./RecruiterLayout";
import { StudentLayout } from "./StudentLayout";
import { DesignSystemPage, NotFoundPage, ScorePage, StudentQuestionPage } from "./pages";
import { AssessmentPage } from "../questions/AssessmentPage";
import { QuestionsPage, QuestionsStartPage } from "../questions/QuestionsPage";
import { ScoreResultPage } from "../score/ScoreResultPage";
import { UploadPage } from "../upload/UploadPage";

export function App() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/recruiter/upload" replace />} />
      <Route path="/design-system" element={<DesignSystemPage />} />
      <Route path="/student" element={<StudentLayout />}>
        <Route path="question" element={<StudentQuestionPage />} />
      </Route>
      <Route element={<RecruiterLayout />}>
        <Route path="/recruiter/upload" element={<UploadPage />} />
        <Route path="/recruiter/score" element={<ScorePage />} />
        <Route path="/recruiter/score/:id" element={<ScoreResultPage />} />
        <Route path="/recruiter/questions" element={<QuestionsStartPage />} />
        <Route path="/recruiter/questions/:id" element={<QuestionsPage />} />
        <Route path="/recruiter/assessment/:id" element={<AssessmentPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
