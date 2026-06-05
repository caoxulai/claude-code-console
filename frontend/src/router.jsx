import { createBrowserRouter } from 'react-router-dom';
import Layout from './components/Layout';
import ChatPage from './pages/ChatPage';
import SettingsPage from './pages/SettingsPage';
import MemoryPage from './pages/MemoryPage';
import SkillsPage from './pages/SkillsPage';
import HooksPage from './pages/HooksPage';
import McpPage from './pages/McpPage';
import CronsPage from './pages/CronsPage';
import SessionsPage from './pages/SessionsPage';
import TasksPage from './pages/TasksPage';
import PluginsPage from './pages/PluginsPage';
import DashboardPage from './pages/DashboardPage';
import ProjectsPage from './pages/ProjectsPage';

export const router = createBrowserRouter([
  {
    path: '/',
    element: <Layout />,
    children: [
      { index: true, element: <DashboardPage /> },
      { path: 'projects', element: <ProjectsPage /> },
      { path: 'chat', element: <ChatPage /> },
      { path: 'settings', element: <SettingsPage /> },
      { path: 'memory', element: <MemoryPage /> },
      { path: 'skills', element: <SkillsPage /> },
      { path: 'hooks', element: <HooksPage /> },
      { path: 'mcp', element: <McpPage /> },
      { path: 'crons', element: <CronsPage /> },
      { path: 'sessions', element: <SessionsPage /> },
      { path: 'tasks', element: <TasksPage /> },
      { path: 'plugins', element: <PluginsPage /> },
    ],
  },
]);
