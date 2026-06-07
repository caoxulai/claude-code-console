/* eslint-disable react-refresh/only-export-components --
   This is the route-config module: it intentionally exports `router` (data)
   alongside lazy() component bindings. The react-refresh HMR rule doesn't apply
   to a non-component module like this. */
import { createBrowserRouter } from 'react-router-dom';
import { lazy, Suspense } from 'react';
import Layout from './components/Layout';

// Route components are lazy-loaded so the initial bundle only carries the
// shell + the first route. Heavy, page-specific deps (react-markdown,
// highlight.js) then load on demand when a page is first visited rather than
// up front for everyone.
const DashboardPage = lazy(() => import('./pages/DashboardPage'));
const ProjectsPage = lazy(() => import('./pages/ProjectsPage'));
const ChatPage = lazy(() => import('./pages/ChatPage'));
const SettingsPage = lazy(() => import('./pages/SettingsPage'));
const MemoryPage = lazy(() => import('./pages/MemoryPage'));
const SkillsPage = lazy(() => import('./pages/SkillsPage'));
const HooksPage = lazy(() => import('./pages/HooksPage'));
const McpPage = lazy(() => import('./pages/McpPage'));
const CronsPage = lazy(() => import('./pages/CronsPage'));
const SessionsPage = lazy(() => import('./pages/SessionsPage'));
const TasksPage = lazy(() => import('./pages/TasksPage'));
const PluginsPage = lazy(() => import('./pages/PluginsPage'));

function lazyRoute(Component) {
  return (
    <Suspense fallback={<div className="loading">Loading…</div>}>
      <Component />
    </Suspense>
  );
}

export const router = createBrowserRouter([
  {
    path: '/',
    element: <Layout />,
    children: [
      { index: true, element: lazyRoute(DashboardPage) },
      { path: 'projects', element: lazyRoute(ProjectsPage) },
      { path: 'chat', element: lazyRoute(ChatPage) },
      { path: 'settings', element: lazyRoute(SettingsPage) },
      { path: 'memory', element: lazyRoute(MemoryPage) },
      { path: 'skills', element: lazyRoute(SkillsPage) },
      { path: 'hooks', element: lazyRoute(HooksPage) },
      { path: 'mcp', element: lazyRoute(McpPage) },
      { path: 'crons', element: lazyRoute(CronsPage) },
      { path: 'sessions', element: lazyRoute(SessionsPage) },
      { path: 'tasks', element: lazyRoute(TasksPage) },
      { path: 'plugins', element: lazyRoute(PluginsPage) },
    ],
  },
]);
