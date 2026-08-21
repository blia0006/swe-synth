#!/usr/bin/env python3
"""
云资源探测（只读）
==================

有了腾讯云 SecretId/SecretKey 之后，用 OpenAPI 把「账号里到底有什么」查清楚，
自动产出可直接填进 .env 的值，不用等导师逐个确认。

它回答 PROGRESS.md 里这几个未确认项：
    · 我这个子用户到底有哪些权限？（决定后面哪一步会被卡）
    · 有没有现成的 TCR 企业版实例可复用？域名是什么？
    · AGS 沙箱服务能不能用？已有哪些沙箱工具 / API Key？
    · 有没有忘关的沙箱实例在计费？
    · 有没有载体为 Agent Runtime 的 CAM 角色（拉 TCR 私有镜像用）？

用法：
    python scripts/probe_cloud.py               # 全部探测
    python scripts/probe_cloud.py --only ags    # 只探某一项

安全约定：
    · 凭证只从环境变量 / .env 读取，绝不写进代码
    · SecretId 只回显前 8 位，SecretKey 完全不回显
    · 全程只调 Describe/Get/List 类只读接口，不创建、不修改、不删除任何资源
      （账号为团队共享，遵守「只增不改不删」原则）
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 团队共享账号：资源命名前缀，用于从一堆资源里认出「自己的」
MY_PREFIX = "swe-synth"


# ---------------------------------------------------------------- 基础设施

def load_env() -> None:
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(path)
    except ImportError:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def title(s: str) -> None:
    print("\n" + "=" * 72)
    print(s)
    print("=" * 72)


def kv(k: str, v) -> None:
    print(f"  {k:<26} {v}")


def explain_error(e: Exception) -> str:
    """把腾讯云的报错翻译成人话 —— 权限问题和配置问题要能一眼区分。"""
    msg = str(e)
    code = getattr(e, "code", "") or ""
    if "AuthFailure.SignatureFailure" in msg or "SecretId" in msg and "not exist" in msg:
        return "密钥无效或已禁用 → 去 CAM → 访问密钥 确认 SecretId/SecretKey，注意 90 天未用会被自动禁用"
    if "UnauthorizedOperation" in code or "UnauthorizedOperation" in msg:
        return "子用户无此接口权限 → 需要在自研上云平台补提权限单"
    if "AuthFailure" in code or "AuthFailure" in msg:
        return "鉴权失败 → 检查密钥、系统时间是否正确"
    if "ResourceNotFound" in msg or "InvalidParameter" in msg:
        return "资源不存在或参数不对（也可能是该产品尚未开通）"
    if "not open" in msg.lower() or "未开通" in msg:
        return "该云产品尚未开通 → 去控制台开通，或提单申请"
    return "未归类错误，把完整信息发给导师定位更快"


def client_of(module_name: str, client_cls: str, version: str, region: str):
    """构造某个产品的 SDK client。"""
    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile

    sid, skey = env("TENCENTCLOUD_SECRET_ID"), env("TENCENTCLOUD_SECRET_KEY")
    cred = credential.Credential(sid, skey)
    hp = HttpProfile(reqTimeout=30)
    cp = ClientProfile(httpProfile=hp)

    # client 类位于 tencentcloud.<产品>.<版本>.<产品>_client 子模块，包 __init__ 未导出
    mod = __import__(f"tencentcloud.{module_name}.{version}.{module_name}_client",
                     fromlist=[client_cls])
    cls = getattr(mod, client_cls)
    return cls(cred, region, cp)


def models_of(module_name: str, version: str):
    return __import__(f"tencentcloud.{module_name}.{version}.models",
                      fromlist=["models"])


# ---------------------------------------------------------------- 1. 身份与权限

def probe_cam(region: str) -> dict:
    title("1. 身份与权限（CAM）")
    out: dict = {}
    try:
        cli = client_of("cam", "CamClient", "v20190116", region)
        m = models_of("cam", "v20190116")
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ 初始化失败：{e}")
        return out

    # 1.1 我是谁
    uin = owner_uin = None
    try:
        rsp = cli.GetUserAppId(m.GetUserAppIdRequest())
        uin, owner_uin = rsp.Uin, rsp.OwnerUin
        out["uin"], out["owner_uin"], out["app_id"] = uin, owner_uin, rsp.AppId
        kv("子用户 Uin", uin)
        kv("主账号 OwnerUin", owner_uin)
        kv("AppId", rsp.AppId)
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ GetUserAppId 失败：{e}")
        print(f"     → {explain_error(e)}")
        return out  # 连身份都查不到，后面必然全败

    # 1.2 我有哪些策略 —— 决定后续每一步会不会被卡
    try:
        req = m.ListAttachedUserAllPoliciesRequest()
        req.TargetUin = int(uin)
        req.AttachType = 0  # 0 = 直接关联到用户
        req.Page, req.Rp = 1, 200
        rsp = cli.ListAttachedUserAllPolicies(req)
        names = [p.PolicyName for p in (rsp.PolicyList or [])]
        out["policies"] = names
        kv("已关联策略数", len(names))
        for n in names:
            print(f"      · {n}")

        # 对照课题需要的能力逐项核验
        need = {
            "TCR/CCR 镜像仓库": ("tcr", "ccr", "container"),
            "AGS Agent 沙箱": ("ags", "sandbox", "agent"),
            "CAM 角色管理": ("cam", "role"),
            "CVM 构建机": ("cvm",),
        }
        print("\n  能力核验（关键字匹配，仅作参考）：")
        joined = " ".join(names).lower()
        for cap, kws in need.items():
            hit = any(k in joined for k in kws)
            full = "AdministratorAccess" in joined or "QCloudResourceFullAccess" in joined
            mark = "✅" if (hit or full) else "❓"
            kv(f"    {mark} {cap}", "疑似已授权" if (hit or full) else "未在策略名中识别到（可能靠用户组继承）")
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  策略列表查询失败：{e}")
        print(f"     → {explain_error(e)}（不影响后续探测，控制台也能看）")

    # 1.3 有没有能给 Agent Runtime 用的角色（拉 TCR 私有镜像的关键）
    try:
        req = m.DescribeRoleListRequest()
        req.Page, req.Rp = 1, 200
        rsp = cli.DescribeRoleList(req)
        roles = rsp.List or []
        kv("\n  账号内角色总数", len(roles))
        cand = [r for r in roles
                if any(k in (r.RoleName or "").lower() for k in ("ags", "agent", "sandbox", MY_PREFIX))]
        if cand:
            print("  与本课题相关的角色候选：")
            for r in cand:
                arn = f"qcs::cam::uin/{owner_uin}:roleName/{r.RoleName}"
                print(f"      · {r.RoleName}")
                print(f"        AGS_ROLE_ARN={arn}")
            out["role_candidates"] = [r.RoleName for r in cand]
        else:
            print("  未发现 ags/agent/sandbox 相关角色")
            print("     → 需在 CAM → 角色 新建：载体选「云产品服务」→ Agent Runtime，")
            print("       并授予 TCR 拉取权限；同时给自己加 cam:PassRole 指向该角色")
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  角色列表查询失败：{e}")
        print(f"     → {explain_error(e)}")

    return out


# ---------------------------------------------------------------- 2. 镜像仓库

def probe_tcr(region: str) -> dict:
    title("2. 容器镜像服务（TCR 企业版 / CCR 个人版）")
    out: dict = {}
    try:
        cli = client_of("tcr", "TcrClient", "v20190924", region)
        m = models_of("tcr", "v20190924")
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ 初始化失败：{e}")
        return out

    # 2.1 企业版实例（团队通常已有现成的，优先复用）
    instances = []
    try:
        req = m.DescribeInstancesRequest()
        req.Offset, req.Limit, req.AllRegion = 0, 100, True
        rsp = cli.DescribeInstances(req)
        instances = rsp.Registries or []
        kv("企业版实例数（全地域）", rsp.TotalCount)
        for i in instances:
            print(f"\n      实例：{i.RegistryName}")
            kv("        RegistryId", i.RegistryId)
            kv("        公网域名", i.PublicDomain)
            kv("        地域", getattr(i, "RegionName", "?"))
            kv("        状态", i.Status)
            kv("        规格", getattr(i, "RegistryType", "?"))
        out["instances"] = [
            {"id": i.RegistryId, "name": i.RegistryName,
             "domain": i.PublicDomain, "region": getattr(i, "RegionName", ""),
             "status": i.Status}
            for i in instances
        ]
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  企业版实例查询失败：{e}")
        print(f"     → {explain_error(e)}")

    # 2.2 已有实例 → 看命名空间，确认能不能建/复用 swe-synth
    for i in instances:
        if str(i.Status).lower() not in ("running", "normal", "success"):
            continue
        try:
            req = m.DescribeNamespacesRequest()
            req.RegistryId = i.RegistryId
            req.Limit, req.Offset, req.All = 100, 0, True
            rsp = cli.DescribeNamespaces(req)
            nss = [n.Name for n in (rsp.NamespaceList or [])]
            print(f"\n      {i.RegistryName} 的命名空间（{len(nss)} 个）：")
            print("        " + (", ".join(nss[:30]) if nss else "（无）"))
            if MY_PREFIX in nss:
                print(f"        ✅ 已存在 {MY_PREFIX} 命名空间，可直接用")
            else:
                print(f"        → 需要新建命名空间 {MY_PREFIX}（控制台一键，或提单）")
        except Exception as e:  # noqa: BLE001
            print(f"      ⚠️  命名空间查询失败（{i.RegistryName}）：{e}")

    # 2.3 没有企业版就看个人版 CCR（免费，练手够用）
    if not instances:
        print("\n  未发现企业版实例，检查个人版 CCR：")
        try:
            req = m.DescribeNamespacePersonalRequest()
            req.Namespace, req.Limit, req.Offset = "", 100, 0
            rsp = cli.DescribeNamespacePersonal(req)
            data = getattr(rsp, "Data", None)
            nss = getattr(data, "NamespaceInfo", None) or []
            kv("    个人版命名空间数", len(nss))
            for n in nss:
                print(f"      · {n.Namespace}")
            print("\n      个人版可用时，.env 这样填：")
            print("        TCR_REGISTRY=ccr.ccs.tencentyun.com")
            print("        TCR_REGISTRY_TYPE=personal")
            out["personal_namespaces"] = [n.Namespace for n in nss]
        except Exception as e:  # noqa: BLE001
            print(f"    ⚠️  个人版查询失败：{e}")
            print(f"       → {explain_error(e)}")

    return out


# ---------------------------------------------------------------- 3. Agent 沙箱

def probe_ags(region: str) -> dict:
    """最关键的一项：AGS 用不了，整个 Agent2 就没地方跑。"""
    title("3. Agent 沙箱服务（AGS）—— 全链路最关键")
    out: dict = {}
    try:
        cli = client_of("ags", "AgsClient", "v20250920", region)
        m = models_of("ags", "v20250920")
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ 初始化失败：{e}")
        return out

    reachable = False

    # 3.1 API Key（就是 .env 里的 E2B_API_KEY）
    try:
        rsp = cli.DescribeAPIKeyList(m.DescribeAPIKeyListRequest())
        reachable = True
        keys = getattr(rsp, "APIKeySet", None) or getattr(rsp, "APIKeyList", None) or []
        kv("已有 API Key 数", len(keys))
        for k in keys:
            name = getattr(k, "APIKeyName", None) or getattr(k, "Name", "?")
            kid = getattr(k, "APIKeyId", None) or getattr(k, "KeyId", "?")
            print(f"      · {name}  (id={kid})")
        if not keys:
            print("     → 需要新建：AGS 控制台 → API Keys → 新建，值填入 .env 的 E2B_API_KEY")
        else:
            print("     ⓘ 已有 Key 的明文一般只在创建时显示一次；")
            print("       拿不到明文就新建一个自己专用的（建议名字带 swe-synth）")
        out["api_keys"] = len(keys)
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ API Key 列表查询失败：{e}")
        print(f"     → {explain_error(e)}")

    # 3.2 沙箱工具（= E2B 的 template）
    try:
        req = m.DescribeSandboxToolListRequest()
        req.Offset, req.Limit = 0, 100
        rsp = cli.DescribeSandboxToolList(req)
        reachable = True
        tools = rsp.SandboxToolSet or []
        kv("\n  已有沙箱工具数", rsp.TotalCount)
        for t in tools:
            name = getattr(t, "ToolName", "?")
            print(f"      · {name}")
            kv("        ToolId", getattr(t, "ToolId", "?"))
            kv("        类型", getattr(t, "ToolType", "?"))
            kv("        状态", getattr(t, "Status", "?"))
        if tools:
            print("\n     ⓘ AGS_SANDBOX_TEMPLATE 填上面某个 ToolName（E2B 的 template = 工具名称）")
        else:
            print("     → 还没有工具。先在控制台建一个内置 code-interpreter 工具练手，")
            print("       跑通后再建自定义镜像工具（那一步才会踩 PassRole 的坑）")
        out["tools"] = [getattr(t, "ToolName", "?") for t in tools]
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ 沙箱工具列表查询失败：{e}")
        print(f"     → {explain_error(e)}")

    # 3.3 运行中的实例 —— 按时长计费，别有忘关的
    try:
        req = m.DescribeSandboxInstanceListRequest()
        req.Offset, req.Limit = 0, 100
        rsp = cli.DescribeSandboxInstanceList(req)
        reachable = True
        insts = getattr(rsp, "SandboxInstanceSet", None) or []
        kv("\n  沙箱实例数", getattr(rsp, "TotalCount", len(insts)))
        alive = [i for i in insts
                 if str(getattr(i, "Status", "")).lower() in ("running", "active", "pending")]
        for i in insts:
            print(f"      · {getattr(i, 'InstanceId', '?')}  状态={getattr(i, 'Status', '?')}")
        if alive:
            print(f"     ⚠️  有 {len(alive)} 个实例仍在运行 —— 按时长计费，确认是否该关掉")
        out["instances_alive"] = len(alive)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  实例列表查询失败：{e}")
        print(f"     → {explain_error(e)}")

    print()
    if reachable:
        print("  ✅ AGS 接口可访问 —— 最大的未知项已排除，Agent2 有地方跑")
    else:
        print("  ❌ AGS 接口完全不可访问 —— 这是最高优先级阻塞项")
        print("     → 确认产品是否已开通 + 子用户是否有 AGS 权限，尽快提单")
    return out


# ---------------------------------------------------------------- 4. TokenHub

def probe_tokenhub(region: str) -> dict:
    """TokenHub 是腾讯云正式产品（有 OpenAPI），API Key 可自助创建，无需问导师。"""
    title("4. TokenHub LLM 网关")
    out: dict = {}
    try:
        cli = client_of("tokenhub", "TokenhubClient", "v20260322", region)
        m = models_of("tokenhub", "v20260322")
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ 初始化失败：{e}")
        print("     → SDK 版本过旧时可能无 tokenhub 模块：pip install -U tencentcloud-sdk-python")
        return out

    # 4.1 已有 API Key（明文一律打码，只能看后 4 位）
    try:
        req = m.DescribeApiKeyListRequest()
        req.Limit, req.Offset = 100, 0
        rsp = cli.DescribeApiKeyList(req)
        data = json.loads(rsp.to_json_string())
        keys = data.get("ApiKeySet") or []
        kv("账号内 API Key 总数", data.get("TotalCount"))
        print("     ⓘ TokenHub 已开通。列表接口返回的 ApiKey 是打码值（如 sk-pB***KZxE），")
        print("       不能直接用；要自己创建一个才能拿到完整明文")
        mine = [k for k in keys if MY_PREFIX in (k.get("Name") or "").lower()]
        if mine:
            print(f"\n     ✅ 已有属于本课题的 Key（名字含 {MY_PREFIX}）：")
            for k in mine:
                print(f"        · {k.get('Name')}  id={k.get('ApiKeyId')}  状态={k.get('Status')}")
            print("        明文可用 DescribeTokenPlanApiKeySecret 尝试读回，或直接新建一个")
        else:
            print(f"\n     → 还没有自己的 Key。创建方式二选一：")
            print("        a) 控制台：https://console.cloud.tencent.com/tokenhub/apikey")
            print("        b) OpenAPI：CreateApiKey(ApiKeyName='swe-synth-xxx', Platform='maas')")
        out["api_key_total"] = data.get("TotalCount")
        out["mine"] = [k.get("Name") for k in mine]
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ API Key 列表查询失败：{e}")
        print(f"     → {explain_error(e)}")
        return out

    # 4.2 可用模型 —— 确认方案里写的模型名真实存在，别等到调用时才发现名字不对
    try:
        allm: list = []
        off = 0
        while True:
            req = m.DescribeModelListRequest()
            req.Limit, req.Offset = 100, off
            d = json.loads(cli.DescribeModelList(req).to_json_string())
            batch = d.get("ModelSet") or []
            allm += batch
            if not batch or len(allm) >= (d.get("TotalCount") or 0):
                break
            off += 100

        kv("\n  可用模型总数", len(allm))
        ids = {(x.get("ModelId") or "").lower() for x in allm}
        names = {(x.get("ModelName") or "").lower() for x in allm}

        want = env("TOKENHUB_MODEL", "deepseek-v4-pro")
        print(f"\n  .env 配置的模型 {want!r}："
              f"{'✅ 存在' if want.lower() in ids | names else '❌ 不存在，需改 TOKENHUB_MODEL'}")

        def price(x) -> str:
            try:
                items = (x.get("ModelChargingInfo") or [{}])[0].get("ChargingItems") or []
                p = {i["PriceName"]: i["Price"] for i in items}
                return f"in {p.get('Input', '?')} / out {p.get('Output', '?')}"
            except Exception:  # noqa: BLE001
                return "?"

        print("\n  适合本课题的候选模型（元/百万 tokens）：")
        for x in sorted(allm, key=lambda a: (a.get("Brand") or "", a.get("ModelId") or "")):
            mid = (x.get("ModelId") or "").lower()
            if x.get("ModelType") != "Text":
                continue
            if not any(k in mid for k in ("deepseek", "glm", "kimi", "hy3")):
                continue
            flag = "" if x.get("Status") == "online" else f"  ⚠️{x.get('Status')}"
            print(f"      {mid:<28} {price(x)}{flag}")
        out["models_total"] = len(allm)
        out["model_ok"] = want.lower() in ids | names
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️  模型列表查询失败：{e}")

    return out


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="云资源探测（只读，不创建任何资源）")
    ap.add_argument("--only", choices=["cam", "tcr", "ags", "tokenhub"], help="只探测某一项")
    ap.add_argument("--json", metavar="PATH", help="把结构化结果写入 JSON 文件")
    args = ap.parse_args()

    load_env()
    sid, skey = env("TENCENTCLOUD_SECRET_ID"), env("TENCENTCLOUD_SECRET_KEY")
    region = env("TENCENTCLOUD_REGION", "ap-guangzhou")

    if not sid or not skey:
        print("❌ 未配置 TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY")
        print("   请编辑 .env 填入（控制台 → 访问管理 → 访问密钥 → API 密钥管理）")
        print("   注意：密钥只写进 .env，不要粘贴到聊天或截图里")
        return 1

    print(f"使用密钥 {sid[:8]}****（SecretKey 不回显） 地域 {region}")

    try:
        import tencentcloud  # noqa: F401
    except ImportError:
        print("❌ 缺少 tencentcloud-sdk-python：pip install -r requirements.txt")
        return 1

    probes = {"cam": probe_cam, "tcr": probe_tcr, "ags": probe_ags,
              "tokenhub": probe_tokenhub}
    names = [args.only] if args.only else list(probes)

    result: dict = {"region": region}
    for n in names:
        try:
            result[n] = probes[n](region)
        except Exception as e:  # noqa: BLE001
            print(f"\n❌ {n} 探测异常：{type(e).__name__}: {e}")
            result[n] = {"error": str(e)}

    title("探测完毕 · 接下来做什么")
    print("""
  1. 把上面查到的值填进 .env：
       TCR_REGISTRY / TCR_NAMESPACE   ← 第 2 节的实例域名与命名空间
       AGS_SANDBOX_TEMPLATE           ← 第 3 节的沙箱工具名称
       AGS_ROLE_ARN                   ← 第 1 节的角色 ARN
       E2B_API_KEY                    ← AGS 控制台新建（明文只显示一次）
       TOKENHUB_API_KEY               ← 第 4 节：控制台自助创建，无需问导师
                                        https://console.cloud.tencent.com/tokenhub/apikey
       GITHUB_TOKEN                   ← GitHub 自助生成（public_repo 读权限即可）

  2. 跑 M0 门禁：python scripts/check_env.py

  3. 门禁绿了就进 M1 单题打样（离线内核可以先并行开发，不必等云）
""")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        print(f"  结构化结果已写入 {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
