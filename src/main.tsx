import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';
import { AppRouter } from './app/router';
import { queryClient } from './app/query-client';
import {
  ReviewRuntimeProvider,
  createReviewRuntime,
  resolveReviewRuntimeConfiguration,
} from './modules/final-cut-review/entry/runtime';
import './app/standalone.css';
import './modules/final-cut-review/styles/fj-review.css';

const container = document.getElementById('root');

if (!container) {
  throw new Error('Missing #root container');
}

const runtimeConfiguration = resolveReviewRuntimeConfiguration(
  import.meta.env.VITE_FINAL_CUT_REVIEW_API_BASE_URL,
  import.meta.env.PROD,
);

container.dataset.reviewRuntime = runtimeConfiguration.runtimeKind;

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ReviewRuntimeProvider
        runtime={createReviewRuntime({
          apiBaseUrl: runtimeConfiguration.apiBaseUrl,
          persistMockRuntime: import.meta.env.DEV,
        })}
      >
        <BrowserRouter>
          <AppRouter />
        </BrowserRouter>
      </ReviewRuntimeProvider>
    </QueryClientProvider>
  </StrictMode>,
);
