// Reading src/styles.css, for the tests that hold it and the guides to their rules.

// A stylesheet without its comments, each ending at its own close.
export const uncommented = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, "");
