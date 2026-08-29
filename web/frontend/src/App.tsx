import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import Sidebar from "./components/Sidebar";
import ChatPage from "./pages/Chat";
import RunPage from "./pages/Run";
import EvalPage from "./pages/Eval";
import SkillsPage from "./pages/Skills";

export default function App() {
  return (
    <BrowserRouter>
      <div className="app-frame">
        <Sidebar />
        <div className="main-content">
          <Routes>
            <Route path="/" element={<ChatPage />} />
            <Route path="/run" element={<RunPage />} />
            <Route path="/eval" element={<EvalPage />} />
            <Route path="/skills" element={<SkillsPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </div>
      </div>
    </BrowserRouter>
  );
}