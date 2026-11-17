// What the page asks of the browser itself, behind an object the tests can replace a part of:
// jsdom does not reload a page, its location cannot be replaced, and it saves no download.
export const browser = {
  // Loads the page afresh, as the browser's own reload does.
  reload: () => window.location.reload(),
  // Saves what an address answers, as the browser's own download: the answer streams to the
  // disk, named as the server says (Content-Disposition), and a refusal shows only as a failed
  // download in the browser's bar. The page is not left.
  download: (url: string) => {
    const link = document.createElement("a");
    link.href = url;
    link.download = "";
    document.body.append(link);
    link.click();
    link.remove();
  },
};
