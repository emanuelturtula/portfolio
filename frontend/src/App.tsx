import { Route, Routes } from 'react-router-dom';

import { HealthPage } from '@/pages/HealthPage';

/**
 * Application shell: a header plus the route table. The router itself lives in
 * `main.tsx` so that tests can mount this component inside a memory router.
 */
export function App() {
  return (
    <div className="app">
      <header className="app-header">
        <h1>Portfolio</h1>
      </header>
      <main className="app-main">
        <Routes>
          <Route path="/" element={<HealthPage />} />
        </Routes>
      </main>
    </div>
  );
}
