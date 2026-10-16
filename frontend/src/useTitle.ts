import { useEffect } from "react";

import { m } from "./messages";

// The browser tab's title while a page is shown: the page's name, then rcman's. A page that
// does not know its name yet passes null and leaves the title as it was.
export function useTitle(page: string | null) {
  useEffect(() => {
    if (page !== null) document.title = m.pageTitle(page);
  }, [page]);
}
