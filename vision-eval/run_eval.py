#!/usr/bin/env python3
"""wq#299 vision eval: minicpm-v4.6 (local, Nephthys ollama) vs lumen-vision
(LiteLLM -> OpenRouter qwen3-vl-32b). OCR field extraction + condition read +
per-image latency. STAGED eval only — no service/config touched.
"""
import base64, json, time, os, requests

IMGS = "/home/lumen/minerva/vision-eval/imgs"
OUT = "/home/lumen/minerva/vision-eval/results.json"
NEPHTHYS = "http://100.98.74.87:11434"
LITELLM = "http://127.0.0.1:4000"

def key():
    for l in open("/home/lumen/agent/.env"):
        if l.startswith("LITELLM_MASTER_KEY="):
            return l.split("=",1)[1].strip()

PROMPT = ("You are grading a product photo for an eBay listing. Return ONLY JSON "
          '{"ocr_text": "<all readable printed text, model/serial/ratings>", '
          '"model_ids": ["..."], "condition_read": "<new|like-new|used|damaged>", '
          '"damage_flags": ["..."]}. If nothing readable, use empty values.')

GROUND = {
  "spec_label.png": {"expect_ocr": ["T5810","7Q4XMN2","E5-2699","D02X"], "expect_cond": "n/a-label"},
  "condition_crack.png": {"expect_ocr": ["used"], "expect_cond": "damaged", "expect_dmg": "crack"},
  "clean_unit.png": {"expect_ocr": ["WX-220"], "expect_cond": "like-new"},
}

def b64(path):
    return base64.b64encode(open(path,"rb").read()).decode()

def run_minicpm(img_b64):
    t=time.time()
    r=requests.post(f"{NEPHTHYS}/api/generate", json={
        "model":"minicpm-v4.6","prompt":PROMPT,"images":[img_b64],
        "stream":False,"options":{"temperature":0.0,"num_predict":512}}, timeout=180)
    r.raise_for_status()
    return r.json().get("response",""), round(time.time()-t,2)

def run_lumenvision(img_b64):
    t=time.time()
    r=requests.post(f"{LITELLM}/v1/chat/completions",
        headers={"Authorization":f"Bearer {key()}"},
        json={"model":"lumen-vision","max_tokens":1024,"temperature":0.0,
              "messages":[{"role":"user","content":[
                {"type":"text","text":PROMPT},
                {"type":"image_url","image_url":{"url":f"data:image/png;base64,{img_b64}"}}]}]},
        timeout=180)
    r.raise_for_status()
    return r.json()["choices"][0]["message"].get("content",""), round(time.time()-t,2)

def main():
    results={}
    for fn in sorted(os.listdir(IMGS)):
        if not fn.endswith(".png"): continue
        ib=b64(os.path.join(IMGS,fn))
        row={"ground": GROUND.get(fn,{})}
        try:
            txt,lat=run_minicpm(ib); row["minicpm"]={"raw":txt[:800],"latency_s":lat}
        except Exception as e:
            row["minicpm"]={"error":f"{type(e).__name__}: {str(e)[:200]}"}
        try:
            txt,lat=run_lumenvision(ib); row["lumen_vision"]={"raw":txt[:800],"latency_s":lat}
        except Exception as e:
            row["lumen_vision"]={"error":f"{type(e).__name__}: {str(e)[:200]}"}
        results[fn]=row
        print(f"done {fn}")
    json.dump(results, open(OUT,"w"), indent=2)
    print("WROTE", OUT)

if __name__=="__main__":
    main()
