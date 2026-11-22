import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import ts from "typescript";
import { expect, it } from "vitest";

const src = new URL("../../src/", import.meta.url);
const manifest = new URL("../../package.json", import.meta.url);
const hostPackage = /(?:^@deepseek-ai\/|(?:^|\/)talaria-dsh-plugin(?:\/|$)|^@talaria\/dsh-plugin(?:\/|$))/;

// 平台 02 / ADR-0020：兜底页与功能组件不能依赖宿主包。
it("keeps DSH dependencies out of the reusable frontend", () => {
  const imports: string[] = [];
  for (const file of readdirSync(src, { recursive: true, encoding: "utf8" })) {
    if (!/\.tsx?$/.test(file) || /\.test\.tsx?$/.test(file)) continue;
    const path = fileURLToPath(new URL(file, src));
    const source = ts.createSourceFile(path, readFileSync(path, "utf8"), ts.ScriptTarget.Latest, true);
    function visit(node: ts.Node) {
      if (ts.isStringLiteralLike(node) && (
        ts.isImportDeclaration(node.parent) || ts.isExportDeclaration(node.parent) ||
        ts.isExternalModuleReference(node.parent) || ts.isLiteralTypeNode(node.parent) ||
        (ts.isCallExpression(node.parent) && (node.parent.expression.kind === ts.SyntaxKind.ImportKeyword ||
          node.parent.expression.getText(source) === "require"))
      ) && hostPackage.test(node.text)) {
        imports.push(`${file} → ${node.text}`);
      }
      ts.forEachChild(node, visit);
    }
    visit(source);
  }
  const pkg = JSON.parse(readFileSync(manifest, "utf8"));
  for (const name of Object.keys({ ...pkg.dependencies, ...pkg.devDependencies, ...pkg.peerDependencies })) {
    if (hostPackage.test(name)) imports.push(`package.json → ${name}`);
  }
  expect(imports).toEqual([]);
});
