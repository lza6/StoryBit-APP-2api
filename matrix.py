import sys,json,time; sys.path.insert(0,'/d/apk-analysis/storybit/2api')
import probe_client as sb
def short(r,n=160):
    s=json.dumps(r,ensure_ascii=False)
    return s[:n]
tests=[
 ("main","/android/v2/category/getEnableCategory",{"v":140,"cc":"en_US"}),
 ("main","/android/v1/template/getHomeTemplates",{"v":140,"cc":"en_US","page":1,"limit":10,"cat_id":"6086b96657e7114a2cf6dede"}),
 ("main","android/v2/template/getExploreData",{"v":140,"cc":"en_US"}),
 ("main","android/v2/template/getAllTemplatesByCategoryExplore",{"v":140,"cc":"en_US","cat_id":"6086b96657e7114a2cf6dede","page":1,"limit":10}),
 ("main","/android/v2/template/getTemplateDetail",{"v":140,"temp_id":"6adaf20975e1de6565134976"}),
 ("main","/android/v1/template/watchTemplate",{"v":140,"temp_id":"6adaf20975e1de6565134976"}),
 ("main","/android/v1/template/completeVideo",{"v":140,"temp_id":"6adaf20975e1de6565134976"}),
 ("main","/android/v1/template/handleAnimationScale",{"v":140}),
 ("main","/android/v1/notification/getNotification",{"v":140,"date":"2026-10-29","":"","DateAndTimeFirstTimeLaunch":""}),
 ("main","/android/v1/banner/getDashboardBanner",{"v":140}),
 ("main","android/v1/config/coin_data",{"v":140,"cc":"en_US"}),
 ("main","android/v1/config/spinner_data",{"v":140,"cc":"en_US"}),
 ("main","/android/v1/config/vu",{"v":140,"cc":"en_US"}),
 ("main","/android/v1/payment/handlePriceUpdate",{"v":140}),
 ("more","/android/v1/banner/getMyBanners",{"p_name":sb.PKG}),
 ("ai","/android/v1/effects/getExplore",{"pn":sb.PKG}),
 ("ai","/android/v1/processStatus/getProcessStatus",{"process_id":"dead","cropRadio":"1:1","pn":sb.PKG}),
 ("ai","/android/v1/processStatus/cancelProcess",{"process_id":"dead","pn":sb.PKG}),
]
MK=b"lksjfdlnalKjsdKLJghgdkhlaksdhgkS"
for host,path,pl in tests:
    base,key={"main":(sb.API_URL,sb.KEY_MAIN),"more":("https://apps.snovxgames.com:7000",MK),"ai":(sb.AI_URL,sb.KEY_AI)}[host]
    t0=time.time()
    try:
        r=sb.call(path,pl,base=base,key=key,timeout=45)
        print(f"[OK  ] {host:5s} {path:46s} {int((time.time()-t0)*1000):5d}ms {short(r)}")
    except Exception as e:
        print(f"[FAIL] {host:5s} {path:46s} {int((time.time()-t0)*1000):5d}ms {repr(e)[:110]}")
