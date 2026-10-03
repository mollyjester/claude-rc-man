import { type AnchorHTMLAttributes, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import { Link, useParams } from "react-router-dom";

import { type ApiError, api } from "./api";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";

type PageInfo = { slug: string; title: string };

// A link to another page of the guide ("users.md#part") stays inside rcman.
function DocLink({ href = "", children }: AnchorHTMLAttributes<HTMLAnchorElement>) {
  const internal = href.match(/^([a-z0-9-]+)\.md(#.*)?$/);
  if (internal) return <Link to={`/help/${internal[1]}${internal[2] ?? ""}`}>{children}</Link>;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  );
}

export function Docs() {
  const { slug } = useParams();
  const [pages, setPages] = useState<PageInfo[]>([]);
  const [markdown, setMarkdown] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  useEffect(() => {
    api.get<{ pages: PageInfo[] }>("/api/docs").then(
      (r) => setPages(r.pages),
      (e) => setError(asApiError(e)),
    );
  }, []);
  useEffect(() => {
    setMarkdown(null);
    setError(null);
    if (slug) {
      api.get<{ markdown: string }>(`/api/docs/${encodeURIComponent(slug)}`).then(
        (r) => setMarkdown(r.markdown),
        (e) => setError(asApiError(e)),
      );
    }
  }, [slug]);

  return (
    <div className="docs">
      <nav aria-label={m.docsTitle}>
        <ul>
          {pages.map((p) => (
            <li key={p.slug}>
              <Link to={`/help/${p.slug}`}>{p.title}</Link>
            </li>
          ))}
        </ul>
      </nav>
      <article>
        {!slug && <h1>{m.docsTitle}</h1>}
        {markdown !== null && <ReactMarkdown components={{ a: DocLink }}>{markdown}</ReactMarkdown>}
        <ProblemView error={error} />
      </article>
    </div>
  );
}
