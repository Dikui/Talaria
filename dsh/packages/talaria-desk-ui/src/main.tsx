import { createRoot } from "react-dom/client";

import App from "./App";
import { applyTheme, resolveInitialTheme } from "./lib/theme";
import "./styles.css";

applyTheme(resolveInitialTheme());

const container = document.getElementById("root");
if (!container) {
  throw new Error("缺少 #root 容器");
}

createRoot(container).render(<App />);
