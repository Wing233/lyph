import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
export default tseslint.config(
 {ignores:[".next/**","node_modules/**","public/**","tests/**","test-results/**"]},
 js.configs.recommended,
 ...tseslint.configs.recommended,
 {languageOptions:{globals:{...globals.browser,...globals.node}},plugins:{"react-hooks":reactHooks},rules:{"no-empty":["error",{allowEmptyCatch:true}],"react-hooks/rules-of-hooks":"error","react-hooks/exhaustive-deps":"warn"}}
);
