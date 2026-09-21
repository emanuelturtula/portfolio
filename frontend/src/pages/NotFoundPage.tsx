import { Link } from 'react-router-dom';

/** Catch-all route, still behind {@link RequireSession} per the route table. */
export function NotFoundPage() {
  return (
    <section aria-labelledby="not-found-heading" className="state">
      <h2 id="not-found-heading">Page not found</h2>
      <p>
        <Link to="/">Return to the dashboard</Link>
      </p>
    </section>
  );
}
