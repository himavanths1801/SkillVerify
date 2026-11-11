import "@fontsource/inter/300.css";
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/500.css";
import "./styles/tokens.css";
import "./styles/components.css";
import "./styles/preview.css";
import "./styles/app.css";
import "./styles/upload.css";
import "./styles/score.css";
import "./styles/questions.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./app/App";
import { ThemeProvider } from "./ThemeProvider";

// Start in the mode of the page being opened, so a direct visit to /student/* does not fade in from recruiter.
const initialMode = window.location.pathname.startsWith("/student") ? "student" : "recruiter";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider initialMode={initialMode}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </ThemeProvider>
  </StrictMode>,
);
