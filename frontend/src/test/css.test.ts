import { uncommented } from "./css";

test("a stylesheet without its comments keeps everything between them", () => {
  expect(uncommented("a { color: red; } /* one */ b { color: blue; }")).toBe("a { color: red; }  b { color: blue; }");
  expect(uncommented("/* a\n rule { color: red; }\n */.dot { width: 1px; }")).toBe(".dot { width: 1px; }");
});

test("each comment ends at its own close, not the last one", () => {
  expect(uncommented("/* one */ a { } /* two */")).toBe(" a { } ");
});

test("a stylesheet with no comments is left as it is", () => {
  expect(uncommented("a { color: red; }")).toBe("a { color: red; }");
  expect(uncommented("")).toBe("");
});
