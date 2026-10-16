import { type AnchorHTMLAttributes, Fragment, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import { Link, useParams } from "react-router-dom";

import { type ApiError, api } from "./api";
import { splitTables } from "./markdownTables";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";
import { TableScroll } from "./TableScroll";
import { useTitle } from "./useTitle";

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

const COMPONENTS = { a: DocLink };
// A table's cell is one line of Markdown, drawn without a paragraph of its own.
const CELL_COMPONENTS = { a: DocLink, p: Fragment };

// A page's Markdown: its tables drawn here, the rest by the Markdown renderer.
function Page({ markdown }: { markdown: string }) {
  return splitTables(markdown).map((segment, i) =>
    segment.kind === "text" ? (
      // biome-ignore lint/suspicious/noArrayIndexKey: the pieces of one page, replaced whole with it
      <ReactMarkdown key={i} components={COMPONENTS}>
        {segment.text}
      </ReactMarkdown>
    ) : (
      // biome-ignore lint/suspicious/noArrayIndexKey: the pieces of one page, replaced whole with it
      <TableScroll key={i} label={m.tableTitle}>
        <table>
          <thead>
            <tr>
              {segment.head.map((cell, c) => (
                // biome-ignore lint/suspicious/noArrayIndexKey: the cells of a table that is replaced whole
                <th key={c} scope="col">
                  <ReactMarkdown components={CELL_COMPONENTS}>{cell}</ReactMarkdown>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {segment.rows.map((row, r) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: the rows of a table that is replaced whole
              <tr key={r}>
                {row.map((cell, c) => (
                  // biome-ignore lint/suspicious/noArrayIndexKey: the cells of a table that is replaced whole
                  <td key={c}>
                    <ReactMarkdown components={CELL_COMPONENTS}>{cell}</ReactMarkdown>
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </TableScroll>
    ),
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

  // The page's own name once it is known, Help for the index and while a page loads.
  useTitle(slug ? (pages.find((p) => p.slug === slug)?.title ?? m.docsTitle) : m.docsTitle);

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
        {!slug && (
          <>
            <h1>{m.docsTitle}</h1>
            <p>{m.docsIntro}</p>
          </>
        )}
        {markdown !== null && <Page markdown={markdown} />}
        <ProblemView error={error} />
      </article>
    </div>
  );
}
