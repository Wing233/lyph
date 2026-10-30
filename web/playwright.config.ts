import {defineConfig} from "@playwright/test";
import path from "node:path";
const python=process.env.LYPH_TEST_PYTHON||path.join("..","api",".venv",process.platform==="win32"?"Scripts/python.exe":"bin/python");
export default defineConfig({
 testDir:"./tests",workers:1,fullyParallel:false,timeout:60000,
 expect:{timeout:15000},
 use:{baseURL:"http://127.0.0.1:3011",headless:true,trace:"retain-on-failure"},
 reporter:[["list"],["json",{outputFile:"tests/results/e2e.json"}]],
 webServer:[
  {command:`"${python}" tests/api_fixture.py`,url:"http://127.0.0.1:8011/health",timeout:30000,reuseExistingServer:false},
  {command:"npm run build && npm run start -- --port 3011",url:"http://127.0.0.1:3011",timeout:180000,reuseExistingServer:false,env:{API_BASE_URL:"http://127.0.0.1:8011",NEXT_TELEMETRY_DISABLED:"1"}}
 ]
});
