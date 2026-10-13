// ------------------------------------------------------------------ v43: KH1's effect MODELS (kh1_fx v8, fx_mdl.h)
// Thunder's bolts, Aero's wind, Blizzard's crystals... are 3D meshes. kh1_fx copies every primitive KH1 submits while
// an effect-model packet runs: vertices in model space (x y z, packed RGBA (128 = 1.0), u v in 1/16 texels, 2048 = no
// texture), the model matrix A (PS2 screen space like the sprites), colour, GS ALPHA and texture. Here: KH1's latest
// complete frame of batches -> world = v * A * inverse(view * proj) -> Borderlands (same map + spell stretch).
// Included at the end of khfx.inc.
static uint32_t g_khmRefaced = 0;
static ID3D11DepthStencilState* g_dsZ[3] = {};   // v76: KH1 depth states for intro effect meshes   // v48: batches re-faced to Borderlands' camera (total)
extern "C" __declspec(dllexport) void sr_khfx_mdl_stats(int* out) {
    out[0] = g_fxm ? 1 : 0; out[1] = g_khmDrawn; out[2] = g_khmBatches; out[3] = g_fxm ? (int)g_fxm->write : 0;
    out[4] = (int)g_khmRefaced;
}
static bool khfx_mdl_map() {
    if (g_fxm) return true;
    if (GetTickCount64() < g_fxmRetry) return false;
    g_fxmRetry = GetTickCount64() + 2000;
    g_fxmMap = OpenFileMappingW(FILE_MAP_ALL_ACCESS, FALSE, FXM_SHARED_NAME);
    if (!g_fxmMap) return false;
    g_fxm = (FxMdlShared*)MapViewOfFile(g_fxmMap, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(FxMdlShared));
    if (!g_fxm || g_fxm->magic != FXM_MAGIC) {
        if (g_fxm) UnmapViewOfFile(g_fxm);
        g_fxm = nullptr; CloseHandle(g_fxmMap); g_fxmMap = nullptr; return false;
    }
    logf("khfx: SoraFxMdl mapped (effect models)");
    return true;
}
static void khfx_mdl_draw(const float Y[3], const float Z[3], ID3D11DepthStencilView* dsv) {
    const float* C = g_khfxCfg;
    bool on = C[16] > 0.5f && g_khfxCfgT && GetTickCount64() - g_khfxCfgT <= 500 && C[39] < 0.5f;   // [39] 1 = models off
    if (!on) { if (g_fxm) g_fxm->enabled = 0; return; }
    if (!fxvs || !g_fxs || !khfx_mdl_map() || (!g_kh && !kh_open())) return;
    g_fxm->enabled = 1;
    double now = qpc_us();
    uint32_t w = g_fxm->write; if (!w) return;
    const FxMdlBatch* ring = g_fxm->batch;
    uint32_t last = w - 1;
    if (g_frmUse) {                                                   // v77: the latched KH1 frame, exactly
        if (g_frmCur.mdl_end == g_frmCur.mdl_begin || w - g_frmCur.mdl_begin > FXM_BATCHES - 16 || g_frmCur.mdl_end > w) { g_khmDrawn = 0; return; }
    } else
    if (ring[last % FXM_BATCHES].seq != last + 1 || now - (double)ring[last % FXM_BATCHES].time_us > (C[25] > 0 ? C[25] : 70) * 1000.0) {
        g_khmDrawn = 0; return;
    }
    double gap = C[26] > 0 ? C[26] : 5000.0;
    auto run_start = [&](uint32_t e) -> uint32_t {
        uint32_t s = e; int64_t t = ring[e % FXM_BATCHES].time_us;
        while (s > 0 && w - s < FXM_BATCHES - 16) {
            const FxMdlBatch& b = ring[(s - 1) % FXM_BATCHES];
            if (b.seq != s || t - b.time_us > gap) break;
            t = b.time_us; s--;
        }
        return s;
    };
    uint32_t end = last, start = run_start(end);
    if (g_frmUse) { start = g_frmCur.mdl_begin; end = g_frmCur.mdl_end - 1; }
    else if (now - (double)ring[end % FXM_BATCHES].time_us < gap && start > 0) {
        uint32_t pe = start - 1;
        if (ring[pe % FXM_BATCHES].seq == pe + 1 && now - (double)ring[pe % FXM_BATCHES].time_us < 70000.0) { end = pe; start = run_start(pe); }
    }
    sel_dbg(1, start, end);
    float vp[16], ivp[16], ip[16];
    const float* camV = g_frmUse ? g_frmCur.view : (const float*)g_fxs->view;   // v77: the frame's own camera
    const float* camP = g_frmUse ? g_frmCur.proj : (const float*)g_fxs->proj;
    m4rv(camV, camP, vp);
    if (!inv4(vp, ivp)) return;
    if (!inv4(camP, ip)) return;               // v48: screen -> KH1 camera space
    // Borderlands camera: right Y, up Z, forward F = Y x Z (UE3: X fwd, Y right, Z up)
    const float F[3] = {Y[1] * Z[2] - Y[2] * Z[1], Y[2] * Z[0] - Y[0] * Z[2], Y[0] * Z[1] - Y[1] * Z[0]};
    float scK = C[24] > 0 ? C[24] : 1.f;
    bool reface_on = C[40] < 0.5f;                                  // [40] 1 = v48 re-facing off
    const float* K = g_khfxK ? g_khfxK : C; float R2 = C[17] * C[17]; float ambient = C[18] > 0 ? C[18] : 10.f; bool dbg = C[27] > 0.5f;
    if (!g_khmVb) {
        D3D11_BUFFER_DESC b{}; b.ByteWidth = KHM_MAXV * sizeof(FxV); b.Usage = D3D11_USAGE_DYNAMIC; b.BindFlags = D3D11_BIND_VERTEX_BUFFER;
        b.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE; dev->CreateBuffer(&b, nullptr, &g_khmVb);
        if (!g_khmVb) return;
    }
    if (!g_rsNoCull) {
        D3D11_RASTERIZER_DESC rd{}; rd.FillMode = D3D11_FILL_SOLID; rd.CullMode = D3D11_CULL_NONE; rd.DepthClipEnable = TRUE;
        dev->CreateRasterizerState(&rd, &g_rsNoCull);
    }
    if (!g_sampWrap) {   // KH1 model UVs run past 1 (Thunder's bolt: 2..3) and rely on the GS repeating the texture
        D3D11_SAMPLER_DESC sd{}; sd.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
        sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_WRAP; sd.MaxLOD = D3D11_FLOAT32_MAX;
        dev->CreateSamplerState(&sd, &g_sampWrap);
    }
    if (!g_samp4[1]) {   // v50: [1] clamp U, [2] clamp V, [3] both ([0] = g_sampWrap)
        for (int i = 1; i < 4; i++) {
            D3D11_SAMPLER_DESC sd{}; sd.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR; sd.MaxLOD = D3D11_FLOAT32_MAX;
            sd.AddressU = (i & 1) ? D3D11_TEXTURE_ADDRESS_CLAMP : D3D11_TEXTURE_ADDRESS_WRAP;
            sd.AddressV = (i & 2) ? D3D11_TEXTURE_ADDRESS_CLAMP : D3D11_TEXTURE_ADDRESS_WRAP; sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
            dev->CreateSamplerState(&sd, &g_samp4[i]);
        }
    }
    auto warp = [&](float* P_) {                                      // spell stretch (see khfx.inc, cfg[28..38])
        if (C[28] < 0.5f) return;
        float wl = sqrtf(C[33] * C[33] + C[34] * C[34]); if (wl < C[29] + 1.f) return;
        float a = ((P_[0] - C[30]) * C[33] + (P_[1] - C[31]) * C[34]) / wl;
        if (a <= C[29]) return;
        float t = (a - C[29]) / (wl - C[29]);
        P_[0] += t * C[36]; P_[1] += t * C[37]; P_[2] += t * C[38];
    };
    struct Run { ID3D11ShaderResourceView* srv; int kind; int first, count; int sub; int samp; int zs; };
    static std::vector<Run> runs; runs.clear();
    D3D11_MAPPED_SUBRESOURCE ms;
    if (FAILED(ctx->Map(g_khmVb, 0, D3D11_MAP_WRITE_DISCARD, 0, &ms))) return;
    FxV* o = (FxV*)ms.pData; int nv = 0, nb = 0;
    double tnow = now * 1e-6;
    for (uint32_t k = start; k <= end && nv < KHM_MAXV - 64; k++) {
        FxMdlBatch b; memcpy(&b, (const void*)&ring[k % FXM_BATCHES], sizeof b);
        if (b.seq != k + 1 || b.nverts == 0 || b.nverts > 4096) continue;
        float W[16]; m4rv(b.A, ivp, W);
        float cx = W[12], cy = W[13], cz = W[14];
        if (!dbg) {
            float dx = cx - C[21], dy = cy - C[22], dz = cz - C[23];
            if (C[17] > 0 && dx * dx + dy * dy + dz * dz > R2) continue;
            if (khfx_muted(C, cx, cy, cz)) continue;
            // (no "drawn for seconds" filter here: KH1 reuses a small pool of model packets every frame, so the header
            // is no identity - room models are caught by their room textures instead)
        }
        const float(*V)[7] = g_fxm->verts;
        const float* v0 = V[b.first % FXM_VERTS];
        bool textured = (int)v0[4] != 2048 || (int)v0[5] != 2048;
        KhTex* tx = textured ? khfx_tex(b.texobj) : nullptr;
        if (textured && (!tx || (tx->room && !dbg))) continue;
        int blend = ((b.alpha & 0xff) == 0x44 || ((b.alpha >> 6) & 3) == 2) ? 2 : 1;   // v70: 0x88 (no Cd term) = alpha, not add
        int sub = (b.alpha & 0xff) == 0x42;                             // v46: subtractive (Gravity's dark orb)
        // v50: KH1's texture wrap per axis (GS CLAMP, kh1_fx v12): repeat or clamp. Repeating a clamped texture tiled
        // Gravity's lightning band / dark rings into stacks of copies. Older DLL (no mark) = repeat (v45 behaviour).
        int samp = 0;
        if (b.pad == FXM_GS_CLAMP_MARK) {
            uint64_t cl; memcpy(&cl, b.gs + 16, 8);
            int wms = (int)(cl & 3), wmt = (int)((cl >> 2) & 3);
            samp = ((wms == 1 || wms == 2) ? 1 : 0) | ((wmt == 1 || wmt == 2) ? 2 : 0);
        }
        int kind = textured ? blend : (blend == 2 ? 4 : 3);
        // v76: KH1's depth state per batch (kh1_fx v39, FXM_Z_*), honoured in the summon intro (g_khfxK = its own stage).
        // Simba's intro draws the rock with Z writes, then the sky / cloud layers behind it; without writes the sky
        // painted over the rock. zs: 0 test only (old), 1 test + write, 2 write only, 3 neither. Writers clip alpha.
        int zs = 0;
        if (g_khfxK && (b.stride & FXM_Z_VALID) && C[92] < 0.5f) {    // [92] 1 = old (no KH1 depth state)
            bool wr = !(b.stride & FXM_Z_NOWRITE); int ztst = (int)((b.stride >> 19) & 3);
            bool tst = ztst != 1;
            zs = tst ? (wr ? 1 : 0) : (wr ? 2 : 3);
        }
        float kz = (zs == 1 || zs == 2) ? 10.f : 0.f;
        // v51: effect-mesh UVs are 1/4096 of the texture whatever its size (Gravity's band spans exactly 4096 = once;
        // Thunder's 2-bolt sheet: 4096-6144 = left bolt, 6144-8192 = right). The old TEX0-size scale (1/16 texel)
        // repeated textures (Gravity's lightning band x4). [42] 1 = old scale.
        float tw = (float)(1u << ((b.tex0 >> 26) & 15)) * 16.f, th = (float)(1u << ((b.tex0 >> 30) & 15)) * 16.f;
        float uvk = 1.f / 4096.f;
        if (C[42] > 0.5f) uvk = 0.f;
        float br = C[19] > 0 ? C[19] : 1.f, as = C[20] > 0 ? C[20] : 1.f;
        // v48: KH1 builds some effect meshes in its CAMERA space as flat layers parallel to its screen at several depths
        // (Gravity's dark disc: texcommon_dpd12540, model->camera = 0.167 * I). Placed in 3D they fan out into a column
        // of copies seen from Borderlands' camera -> rebuild such a batch around its centre facing Borderlands' camera,
        // KH1 camera offsets (x right, y down, z forward) -> Borderlands right / up / forward.
        float M[16]; m4rv(b.A, ip, M);                                 // model -> KH1 camera (homogeneous)
        auto camv = [&](const float* v, float* o3) {
            float x = v[0] * M[0] + v[1] * M[4] + v[2] * M[8] + M[12], y = v[0] * M[1] + v[1] * M[5] + v[2] * M[9] + M[13];
            float z = v[0] * M[2] + v[1] * M[6] + v[2] * M[10] + M[14], w = v[0] * M[3] + v[1] * M[7] + v[2] * M[11] + M[15];
            if (fabsf(w) > 1e-6f && fabsf(w - 1.f) > 1e-3f) { x /= w; y /= w; z /= w; }
            o3[0] = x; o3[1] = y; o3[2] = z;
        };
        bool reface = false; float cc[3] = {0, 0, 0}, bcw[3] = {0, 0, 0};
        if (reface_on) {
            int per = b.mode == 8 ? 4 : 3, prims = 0, flat = 0;
            for (uint32_t p0 = 0; p0 + per <= b.nverts && prims < 256; p0 += per, prims++) {
                float zmin = 1e30f, zmax = -1e30f, xmin = 1e30f, xmax = -1e30f, ymin = 1e30f, ymax = -1e30f, c3[3];
                for (int e = 0; e < per; e++) {
                    camv(V[(b.first + p0 + e) % FXM_VERTS], c3);
                    zmin = fminf(zmin, c3[2]); zmax = fmaxf(zmax, c3[2]); xmin = fminf(xmin, c3[0]); xmax = fmaxf(xmax, c3[0]);
                    ymin = fminf(ymin, c3[1]); ymax = fmaxf(ymax, c3[1]);
                }
                float ext = fmaxf(xmax - xmin, ymax - ymin);
                if (ext > 1e-4f && zmax - zmin < 0.02f * ext) flat++;
            }
            reface = prims > 0 && flat * 10 >= prims * 8;               // >= 80 % of its pieces face KH1's camera
            if (C[41] > 0.5f) reface = true;                            // v49 [41]: every effect mesh as KH1's camera sees it
            if (reface) {
                float z0[3] = {0, 0, 0}; camv(z0, cc);                  // batch centre (model origin) in KH1 camera space
                float wx = W[12], wy = W[13], wz = W[14], ww = W[15];
                if (fabsf(ww) > 1e-6f && fabsf(ww - 1.f) > 1e-3f) { wx /= ww; wy /= ww; wz /= ww; }
                bcw[0] = wx * K[0] + wy * K[4] + wz * K[8] + K[12]; bcw[1] = wx * K[1] + wy * K[5] + wz * K[9] + K[13];
                bcw[2] = wx * K[2] + wy * K[6] + wz * K[10] + K[14];
                warp(bcw);
                g_khmRefaced++;
            }
        }
        auto emit = [&](uint32_t vi) {
            const float* v = V[vi % FXM_VERTS];
            float P[3];
            if (reface) {
                float c3[3]; camv(v, c3);
                float ox = c3[0] - cc[0], oy = c3[1] - cc[1], oz = c3[2] - cc[2];
                for (int a = 0; a < 3; a++) P[a] = bcw[a] + scK * (ox * Y[a] - oy * Z[a] + oz * F[a]);
            } else {
                float wx = v[0] * W[0] + v[1] * W[4] + v[2] * W[8] + W[12], wy = v[0] * W[1] + v[1] * W[5] + v[2] * W[9] + W[13];
                float wz = v[0] * W[2] + v[1] * W[6] + v[2] * W[10] + W[14], ww = v[0] * W[3] + v[1] * W[7] + v[2] * W[11] + W[15];
                if (fabsf(ww) > 1e-6f && fabsf(ww - 1.f) > 1e-3f) { wx /= ww; wy /= ww; wz /= ww; }
                P[0] = wx * K[0] + wy * K[4] + wz * K[8] + K[12]; P[1] = wx * K[1] + wy * K[5] + wz * K[9] + K[13];
                P[2] = wx * K[2] + wy * K[6] + wz * K[10] + K[14];
                warp(P);
            }
            uint32_t c; memcpy(&c, &v[3], 4);
            FxV& q = o[nv++];
            q.p[0] = P[0]; q.p[1] = P[1]; q.p[2] = P[2]; q.uv[0] = uvk > 0 ? v[4] * uvk : v[4] / tw; q.uv[1] = uvk > 0 ? v[5] * uvk : v[5] / th;
            q.c[0] = (c & 0xff) / 128.f * b.col[0] * br; q.c[1] = ((c >> 8) & 0xff) / 128.f * b.col[1] * br;
            q.c[2] = ((c >> 16) & 0xff) / 128.f * b.col[2] * br; q.c[3] = ((c >> 24) & 0xff) / 128.f * b.col[3] * as;
            if (blend == 2 && q.c[3] > 1.f) q.c[3] = 1.f;
            q.k = (float)kind + kz;
        };
        int first = nv;
        if (b.mode == 8) {                                              // quads in loop order -> (0 1 2) (0 2 3)
            for (uint32_t q4 = 0; q4 + 3 < b.nverts && nv + 6 <= KHM_MAXV; q4 += 4) {
                uint32_t f = b.first + q4;
                emit(f); emit(f + 1); emit(f + 2); emit(f); emit(f + 2); emit(f + 3);
            }
        } else {                                                        // triangle list
            for (uint32_t t3 = 0; t3 + 2 < b.nverts && nv + 3 <= KHM_MAXV; t3 += 3) {
                emit(b.first + t3); emit(b.first + t3 + 1); emit(b.first + t3 + 2);
            }
        }
        ID3D11ShaderResourceView* srv = tx ? tx->srv : nullptr;
        if (nv > first) {
            if (!runs.empty() && runs.back().srv == srv && runs.back().kind == kind && runs.back().sub == sub && runs.back().samp == samp && runs.back().zs == zs) runs.back().count += nv - first;
            else runs.push_back({srv, kind, first, nv - first, sub, samp, zs});
            nb++;
        }
    }
    ctx->Unmap(g_khmVb, 0);
    g_khmDrawn = nv / 3; g_khmBatches = nb;
    if (!nv) return;
    UINT fs = sizeof(FxV), fo = 0;
    ctx->IASetInputLayout(fxil); ctx->IASetVertexBuffers(0, 1, &g_khmVb, &fs, &fo);
    ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx->VSSetShader(fxvs, nullptr, 0); ctx->PSSetShader(fxps, nullptr, 0); ctx->PSSetSamplers(0, 1, g_sampWrap ? &g_sampWrap : &sampClamp);
    if (!g_dsZ[1]) {                                                  // v76: [1] test + write, [2] write only (always)
        D3D11_DEPTH_STENCIL_DESC d{}; d.DepthEnable = TRUE; d.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ALL;
        d.DepthFunc = D3D11_COMPARISON_LESS_EQUAL; dev->CreateDepthStencilState(&d, &g_dsZ[1]);
        d.DepthFunc = D3D11_COMPARISON_ALWAYS; dev->CreateDepthStencilState(&d, &g_dsZ[2]);
    }
    ctx->OMSetDepthStencilState(dsv ? dsFx : dsOff, 0);
    ctx->RSSetState(g_rsNoCull ? g_rsNoCull : rs);                    // effect meshes are seen from both sides
    float bz[4] = {0, 0, 0, 0};
    for (auto& rn : runs) {
        ctx->PSSetShaderResources(0, 1, &rn.srv);
        ID3D11SamplerState* smp = rn.samp && g_samp4[rn.samp] ? g_samp4[rn.samp] : (g_sampWrap ? g_sampWrap : sampClamp);
        ctx->PSSetSamplers(0, 1, &smp);
        bool alpha = rn.kind == 2 || rn.kind == 4;
        ctx->OMSetBlendState(alpha ? bsAlpha : (rn.sub && bsSub ? bsSub : bsAdd), bz, 0xffffffff);
        if (dsv) ctx->OMSetDepthStencilState(rn.zs == 1 || rn.zs == 2 ? (g_dsZ[rn.zs] ? g_dsZ[rn.zs] : dsFx) : (rn.zs == 3 ? dsOff : dsFx), 0);
        ctx->Draw(rn.count, rn.first);
    }
    ctx->RSSetState(rs);
}
