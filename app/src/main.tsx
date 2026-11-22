import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";

if (import.meta.env.DEV && !window.bindery) {
  const { installDevPreview } = await import("./devPreview");
  installDevPreview();
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
