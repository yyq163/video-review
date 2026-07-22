import { lazy, Suspense, type ReactNode } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import { RouteLoadBoundary, RouteLoading } from './RouteLoadBoundary';

const ProjectDetailPage = lazy(async () => {
  const module = await import('../modules/final-cut-review/pages/ProjectDetailPage');
  return { default: module.ProjectDetailPage };
});
const ProjectListPage = lazy(async () => {
  const module = await import('../modules/final-cut-review/pages/ProjectListPage');
  return { default: module.ProjectListPage };
});
const ReviewWorkspacePage = lazy(async () => {
  const module = await import('../modules/final-cut-review/pages/ReviewWorkspacePage');
  return { default: module.ReviewWorkspacePage };
});

function routeElement(element: ReactNode) {
  return (
    <RouteLoadBoundary>
      <Suspense fallback={<RouteLoading />}>{element}</Suspense>
    </RouteLoadBoundary>
  );
}

export function AppRouter() {
  return (
    <Routes>
      <Route path="/" element={<Navigate to="/review/projects" replace />} />
      <Route path="/edit" element={<Navigate to="/edit/projects" replace />} />
      <Route path="/review" element={<Navigate to="/review/projects" replace />} />
      <Route path="/edit/projects" element={routeElement(<ProjectListPage entryMode="edit" />)} />
      <Route path="/edit/projects/:projectRefId" element={routeElement(<ProjectDetailPage entryMode="edit" />)} />
      <Route
        path="/edit/projects/:projectRefId/items/:reviewItemId"
        element={routeElement(<ReviewWorkspacePage entryMode="edit" />)}
      />
      <Route path="/review/projects" element={routeElement(<ProjectListPage entryMode="review" />)} />
      <Route path="/review/projects/:projectRefId" element={routeElement(<ProjectDetailPage entryMode="review" />)} />
      <Route
        path="/review/projects/:projectRefId/items/:reviewItemId"
        element={routeElement(<ReviewWorkspacePage entryMode="review" />)}
      />
      <Route path="*" element={<Navigate to="/review/projects" replace />} />
    </Routes>
  );
}
