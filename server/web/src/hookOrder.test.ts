/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import ts from "typescript";

/**
 * No hook may be called after an early return.
 *
 * This is the `react-hooks/rules-of-hooks` lint rule, which this project has no
 * ESLint to run. It is here because the rule earned it: Console rendered its
 * "Session ended" curtain from an early return placed above `useVideoStream`,
 * so the first render that saw a revoked session called one hook fewer than the
 * one before it and React threw #300 instead. It reached production and flashed
 * on every sign-out.
 *
 * It also survived five hand-written scans of the same file, because a return
 * nested one level inside `if (...) { ... }` is invisible to anything counting
 * indentation or braces. So this walks the real TypeScript AST: the parser
 * already knows what a statement is, what a nested function is, and what is
 * only a brace inside a string or a JSX expression.
 *
 * The check is intentionally conservative. It reports a hook only when an
 * earlier *sibling* statement can return, which is the case React actually
 * fails on - a return in a nested closure, or one in the tail position after
 * every hook, is not flagged.
 */

const HOOK = /^use[A-Z]/;

type Violation = { file: string; line: number; fn: string; hook: string; returnLine: number };

/** Statements that can hand control back before the ones that follow them. */
function containsReturn(node: ts.Node): ts.ReturnStatement | null {
  let found: ts.ReturnStatement | null = null;
  const visit = (n: ts.Node) => {
    if (found) return;
    // A return inside a nested function belongs to that function, not this one.
    if (
      ts.isFunctionDeclaration(n) ||
      ts.isFunctionExpression(n) ||
      ts.isArrowFunction(n) ||
      ts.isClassDeclaration(n)
    ) {
      return;
    }
    if (ts.isReturnStatement(n)) {
      found = n;
      return;
    }
    ts.forEachChild(n, visit);
  };
  ts.forEachChild(node, visit);
  return found;
}

function findHookCalls(node: ts.Node): ts.CallExpression[] {
  const hits: ts.CallExpression[] = [];
  const visit = (n: ts.Node) => {
    if (
      ts.isFunctionDeclaration(n) ||
      ts.isFunctionExpression(n) ||
      ts.isArrowFunction(n) ||
      ts.isClassDeclaration(n)
    ) {
      return; // hooks in a nested function are that function's problem
    }
    if (ts.isCallExpression(n)) {
      const name = ts.isPropertyAccessExpression(n.expression)
        ? n.expression.name.text
        : ts.isIdentifier(n.expression)
          ? n.expression.text
          : "";
      if (HOOK.test(name)) hits.push(n);
    }
    ts.forEachChild(n, visit);
  };
  ts.forEachChild(node, visit);
  return hits;
}

function check(file: string, source: string): Violation[] {
  const sf = ts.createSourceFile(file, source, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TSX);
  const out: Violation[] = [];
  const lineOf = (n: ts.Node) => sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1;

  const inspect = (body: ts.Block, fnName: string) => {
    let seenReturn: ts.ReturnStatement | null = null;
    for (const stmt of body.statements) {
      if (seenReturn) {
        for (const hook of findHookCalls(stmt)) {
          const name = ts.isPropertyAccessExpression(hook.expression)
            ? hook.expression.name.text
            : (hook.expression as ts.Identifier).text;
          out.push({
            file,
            line: lineOf(hook),
            fn: fnName,
            hook: name,
            returnLine: lineOf(seenReturn),
          });
        }
      }
      // A bare trailing `return` is fine; what matters is a return that can fire
      // while statements after it still expect to run.
      if (!seenReturn) {
        seenReturn = ts.isReturnStatement(stmt) ? stmt : containsReturn(stmt);
      }
    }
  };

  const visit = (node: ts.Node) => {
    let name: string | null = null;
    let body: ts.Block | undefined;
    if (ts.isFunctionDeclaration(node) && node.name && node.body) {
      name = node.name.text;
      body = node.body;
    } else if (
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      node.initializer &&
      (ts.isArrowFunction(node.initializer) || ts.isFunctionExpression(node.initializer)) &&
      node.initializer.body &&
      ts.isBlock(node.initializer.body)
    ) {
      name = node.name.text;
      body = node.initializer.body;
    }
    // Components and custom hooks only - React's rule applies to nothing else.
    if (name && body && (HOOK.test(name) || /^[A-Z]/.test(name))) inspect(body, name);
    ts.forEachChild(node, visit);
  };
  ts.forEachChild(sf, visit);
  return out;
}

describe("rules of hooks", () => {
  it("calls no hook after an early return", () => {
    // Vite reads the tree for us, as strings. No node APIs, so this file
    // type-checks in the same `tsc -b` as the app - which has no @types/node -
    // and it resolves identically on Windows and under WSL.
    const sources = import.meta.glob("./**/*.{ts,tsx}", {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;
    const files = Object.entries(sources).filter(([f]) => !/\.test\.tsx?$/.test(f));
    // A glob that matched nothing would pass by checking nothing at all.
    expect(files.length).toBeGreaterThan(20);
    const violations = files.flatMap(([f, src]) => check(f.replace(/^\.\//, ""), src));
    const report = violations.map(
      (v) => `${v.file}:${v.line} ${v.fn}() calls ${v.hook}() after the return on line ${v.returnLine}`,
    );
    expect(report).toEqual([]);
  });

  it("catches the shape that actually shipped", () => {
    // Console's bug, reduced: the curtain returned above the video hook.
    const bug = `
      export function Console({ socket }) {
        const [tab, setTab] = useState("airspace");
        if (socket.revoked) {
          return <div className="curtain">Session ended</div>;
        }
        const streamState = useVideoStream(ref, id, true);
        return <div>{tab}{streamState}</div>;
      }`;
    const found = check("Console.tsx", bug);
    expect(found).toHaveLength(1);
    expect(found[0].hook).toBe("useVideoStream");
  });

  it("does not flag a return that comes after every hook", () => {
    const fine = `
      export function Panel({ compact }) {
        const [a] = useState(0);
        const b = useMemo(() => a, [a]);
        if (compact) return <small>{b}</small>;
        return <big>{b}</big>;
      }`;
    expect(check("Panel.tsx", fine)).toEqual([]);
  });

  it("ignores returns inside nested callbacks", () => {
    const fine = `
      export function List({ items }) {
        const rows = items.map((i) => { if (!i.ok) return null; return i.name; });
        const [sel, setSel] = useState(null);
        return <ul>{rows}{sel}</ul>;
      }`;
    expect(check("List.tsx", fine)).toEqual([]);
  });
});
