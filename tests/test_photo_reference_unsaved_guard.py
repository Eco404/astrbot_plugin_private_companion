# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "pages" / "陪伴面板" / "app.js"


class PhotoReferenceUnsavedGuardTests(unittest.TestCase):
    def test_opening_and_round_tripping_an_unchanged_catalog_stays_clean(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js 不可用")

        script = f"""
const fs = require("fs");
const source = fs.readFileSync({json.dumps(str(APP_JS), ensure_ascii=False)}, "utf8");

function extractFunction(name) {{
  const marker = `function ${{name}}`;
  const start = source.indexOf(marker);
  if (start < 0) return "";
  const paramsOpen = source.indexOf("(", start + marker.length);
  let paramsDepth = 0;
  let paramsClose = -1;
  for (let index = paramsOpen; index < source.length; index += 1) {{
    if (source[index] === "(") paramsDepth += 1;
    if (source[index] === ")" && --paramsDepth === 0) {{
      paramsClose = index;
      break;
    }}
  }}
  if (paramsOpen < 0 || paramsClose < 0) {{
    throw new Error(`无法定位函数参数: ${{name}}`);
  }}
  const open = source.indexOf("{{", paramsClose + 1);
  let depth = 0;
  let quote = "";
  let escaped = false;
  let lineComment = false;
  let blockComment = false;
  for (let index = open; index < source.length; index += 1) {{
    const char = source[index];
    const next = source[index + 1];
    if (lineComment) {{
      if (char === "\\n") lineComment = false;
      continue;
    }}
    if (blockComment) {{
      if (char === "*" && next === "/") {{
        blockComment = false;
        index += 1;
      }}
      continue;
    }}
    if (quote) {{
      if (escaped) {{
        escaped = false;
      }} else if (char === "\\\\") {{
        escaped = true;
      }} else if (char === quote) {{
        quote = "";
      }}
      continue;
    }}
    if (char === "/" && next === "/") {{
      lineComment = true;
      index += 1;
      continue;
    }}
    if (char === "/" && next === "*") {{
      blockComment = true;
      index += 1;
      continue;
    }}
    if (char === "'" || char === '"' || char === "`") {{
      quote = char;
      continue;
    }}
    if (char === "{{") depth += 1;
    if (char === "}}" && --depth === 0) return source.slice(start, index + 1);
  }}
  throw new Error(`无法提取函数: ${{name}}`);
}}

const state = {{
  overview: null,
  featureDraft: {{}},
  featureDetailParamDraft: {{}},
  featureDetailBaseline: null,
  featureDetailDirty: false,
  selectedFeatureKey: "",
  photoReferenceManagerDraft: null,
  photoReferenceLibraryStatus: {{ options: {{}} }},
}};
let markDirtyCalls = 0;
function isProviderConfigKey() {{ return false; }}
function syncFeatureFooterAction() {{}}
function markFeatureDetailDirty() {{
  markDirtyCalls += 1;
  state.featureDetailDirty = true;
}}

const implementation = [
  "toBool",
  "collectSettingValue",
  "cloneFeatureStateValue",
  "normalizePhotoReferenceSource",
  "normalizePhotoReferenceMetadataList",
  "normalizePhotoReferenceMetadataBoolean",
  "photoReferenceMetadataFromObject",
  "newPhotoReferenceId",
  "currentPhotoReferenceCatalogValue",
  "parsePhotoReferenceCatalog",
  "photoReferenceCatalogFromStatus",
  "canonicalPhotoReference",
  "hydratePhotoReferenceDraftFromStatus",
  "currentPhotoPersonaReference",
  "currentPhotoPersonaReferenceValue",
  "serializePhotoReferenceCatalog",
  "photoReferenceManagerItems",
  "rememberFeatureParamDraft",
  "featureDetailFormSignature",
  "refreshFeatureDetailDirty",
  "syncPhotoReferenceManagerDraft",
  "photoReferenceCatalogSignature",
].map(extractFunction).filter(Boolean).join("\\n");
eval(implementation);

if (!source.includes("function photoReferenceCatalogSignature(")) {{
  globalThis.photoReferenceCatalogSignature = function photoReferenceCatalogSignature(value) {{
    return JSON.stringify(parsePhotoReferenceCatalog(value));
  }};
}}

const catalogInput = {{
  dataset: {{ featureParam: "photo_reference_catalog" }},
  type: "textarea",
  value: "",
}};
const featureToggle = {{ checked: true }};
const featurePage = {{
  querySelector(selector) {{
    return selector === "[data-feature-detail-toggle]" ? featureToggle : null;
  }},
  querySelectorAll(selector) {{
    return selector === "[data-feature-param]" ? [catalogInput] : [];
  }},
}};
const document = {{
  querySelector(selector) {{
    if (selector === ".feature-detail-page") return featurePage;
    if (selector === '[data-feature-param="photo_reference_catalog"]') return catalogInput;
    return null;
  }},
  querySelectorAll(selector) {{
    return selector === "[data-feature-param]" ? [catalogInput] : [];
  }},
}};

const persona = {{
  id: "persona",
  kind: "persona",
  source: "C:/references/persona.png",
  note: "persona",
  reference_roles: ["identity"],
  outfit_category: "",
  outfit_lock_default: false,
  scene_categories: [],
  time_categories: [],
  preferred_preset: "",
  metadata_source: "configured",
}};
const library = {{
  id: "library_1",
  kind: "library",
  source: "C:/references/home.png",
  note: "home",
  reference_roles: ["outfit"],
  outfit_category: "homewear",
  outfit_lock_default: false,
  scene_categories: ["home"],
  time_categories: ["evening"],
  preferred_preset: "",
  metadata_source: "configured",
}};
const savedCatalog = [persona, library];

state.selectedFeatureKey = "enable_photo_text_action";
state.featureDraft = {{ enable_photo_text_action: true }};
state.overview = {{ settings: {{ photo_reference_catalog: savedCatalog }} }};
state.featureDetailParamDraft = {{}};

catalogInput.value = savedCatalog.map((item) => JSON.stringify(item)).join("\\n");
rememberFeatureParamDraft(catalogInput);
if (Object.prototype.hasOwnProperty.call(state.featureDetailParamDraft, "photo_reference_catalog")) {{
  throw new Error("打开参考图库不应写入 photo_reference_catalog 通用草稿");
}}

state.featureDetailParamDraft = {{}};
state.featureDetailDirty = false;
state.photoReferenceManagerDraft = parsePhotoReferenceCatalog(savedCatalog)
  .filter((item) => item.kind === "library");
catalogInput.value = JSON.stringify(savedCatalog);
state.featureDetailBaseline = {{
  key: state.selectedFeatureKey,
  settings: {{ photo_reference_catalog: savedCatalog }},
  formSignature: featureDetailFormSignature(document),
}};
markDirtyCalls = 0;
syncPhotoReferenceManagerDraft();
if (markDirtyCalls !== 0 || state.featureDetailDirty) {{
  throw new Error("未修改返回参考图库不应触发脏状态");
}}

state.photoReferenceManagerDraft[0].note = "changed";
syncPhotoReferenceManagerDraft();
if (
  markDirtyCalls !== 1
  || !state.featureDetailDirty
  || !Object.prototype.hasOwnProperty.call(state.featureDetailParamDraft, "photo_reference_catalog")
) {{
  throw new Error("真实修改参考图库后必须写入草稿并触发脏状态");
}}

const lineValue = savedCatalog.map((item) => JSON.stringify(item)).join("\\n");
catalogInput.value = lineValue;
const lineSignature = featureDetailFormSignature(document);
catalogInput.value = JSON.stringify(savedCatalog);
const arraySignature = featureDetailFormSignature(document);
if (lineSignature !== arraySignature) {{
  throw new Error("目录的 textarea 表示和 JSON 数组表示应得到相同表单签名");
}}

state.featureDetailParamDraft = {{}};
state.featureDetailDirty = false;
state.overview.settings.photo_reference_catalog = [];
state.featureDetailBaseline = {{
  key: state.selectedFeatureKey,
  settings: {{ photo_reference_catalog: [] }},
  formSignature: "stale-signature",
}};
state.photoReferenceManagerDraft = null;
const status = {{
  persona,
  items: [library],
}};
const statusCatalog = photoReferenceCatalogFromStatus(status);
if (!hydratePhotoReferenceDraftFromStatus(status)) {{
  throw new Error("未修改状态回填应被接受");
}}
if (JSON.stringify(state.featureDetailBaseline.settings.photo_reference_catalog) !== JSON.stringify(statusCatalog)) {{
  throw new Error("状态回填后详情基线应包含当前目录");
}}
if (state.featureDetailBaseline.formSignature !== "") {{
  throw new Error("状态回填后应重新捕获详情页干净基线");
}}

process.stdout.write("ok");
"""
        result = subprocess.run(
            [node, "-e", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"Node 回归测试失败:\nstdout={result.stdout}\nstderr={result.stderr}",
        )
        self.assertEqual(result.stdout, "ok")


if __name__ == "__main__":
    unittest.main()
