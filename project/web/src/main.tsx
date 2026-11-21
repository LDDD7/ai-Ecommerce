import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";
import "./styles-diagnosis.css";
import "./styles-settings.css";
import "./styles-actions.css";

const root = document.getElementById("root");
if (!root) throw new Error("找不到挂载点 #root");

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
