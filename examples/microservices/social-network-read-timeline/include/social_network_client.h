#pragma once
#include <string>
#include <map>
#include <vector>
#include <chrono>
#include <thread>
#include <sstream>
#include <curl/curl.h>
#include <nlohmann/json.hpp>

using json = nlohmann::json;

struct HttpResponse {
    int status;
    std::string body;
    std::map<std::string, std::string> headers;
    double latency_ms;
};

static size_t writeCallback(void* contents, size_t size, size_t nmemb, std::string* s) {
    s->append(static_cast<char*>(contents), size * nmemb);
    return size * nmemb;
}

static size_t headerCallback(char* buffer, size_t size, size_t nitems,
                              std::map<std::string,std::string>* headers) {
    std::string line(buffer, size * nitems);
    auto colon = line.find(':');
    if (colon != std::string::npos) {
        std::string key = line.substr(0, colon);
        std::string val = line.substr(colon + 1);
        while (!val.empty() && (val.front()==' '||val.front()=='\t')) val.erase(0,1);
        while (!val.empty() && (val.back()=='\r'||val.back()=='\n'||val.back()==' ')) val.pop_back();
        (*headers)[key] = val;
    }
    return size * nitems;
}

inline HttpResponse httpGet(const std::string& url, int timeout_ms = 10000) {
    HttpResponse resp; resp.status = 0; resp.latency_ms = 0;
    CURL* curl = curl_easy_init();
    if (!curl) return resp;
    auto t0 = std::chrono::high_resolution_clock::now();
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, writeCallback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &resp.body);
    curl_easy_setopt(curl, CURLOPT_HEADERFUNCTION, headerCallback);
    curl_easy_setopt(curl, CURLOPT_HEADERDATA, &resp.headers);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, timeout_ms);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    CURLcode res = curl_easy_perform(curl);
    auto t1 = std::chrono::high_resolution_clock::now();
    resp.latency_ms = std::chrono::duration<double,std::milli>(t1-t0).count();
    if (res == CURLE_OK) { long c; curl_easy_getinfo(curl,CURLINFO_RESPONSE_CODE,&c); resp.status=(int)c; }
    curl_easy_cleanup(curl);
    return resp;
}

inline std::string urlEncode(const std::string& s) {
    CURL* curl = curl_easy_init(); std::string r;
    if (curl) { char* e=curl_easy_escape(curl,s.c_str(),(int)s.size()); if(e){r=e;curl_free(e);} curl_easy_cleanup(curl); }
    return r;
}

inline std::string buildForm(const std::map<std::string,std::string>& p) {
    std::string b;
    for (auto& kv:p) { if(!b.empty())b+='&'; b+=urlEncode(kv.first)+'='+urlEncode(kv.second); }
    return b;
}

inline HttpResponse httpPost(const std::string& url,
                              const std::map<std::string,std::string>& form,
                              int timeout_ms = 10000) {
    HttpResponse resp; resp.status = 0; resp.latency_ms = 0;
    CURL* curl = curl_easy_init();
    if (!curl) return resp;
    std::string body = buildForm(form);
    auto t0 = std::chrono::high_resolution_clock::now();
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, writeCallback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &resp.body);
    curl_easy_setopt(curl, CURLOPT_HEADERFUNCTION, headerCallback);
    curl_easy_setopt(curl, CURLOPT_HEADERDATA, &resp.headers);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, timeout_ms);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    CURLcode res = curl_easy_perform(curl);
    auto t1 = std::chrono::high_resolution_clock::now();
    resp.latency_ms = std::chrono::duration<double,std::milli>(t1-t0).count();
    if (res == CURLE_OK) { long c; curl_easy_getinfo(curl,CURLINFO_RESPONSE_CODE,&c); resp.status=(int)c; }
    curl_easy_cleanup(curl);
    return resp;
}

struct SocialNetworkClient {
    std::string base_url;
    explicit SocialNetworkClient(const std::string& u) : base_url(u) {}

    bool registerUser(const std::string& username, const std::string& password,
                      int user_id, const std::string& first, const std::string& last) {
        auto r = httpPost(base_url+"/wrk2-api/user/register",
            {{"username",username},{"password",password},
             {"user_id",std::to_string(user_id)},{"first_name",first},{"last_name",last}});
        return r.status==200 && r.body.find("Success")!=std::string::npos;
    }

    bool followByName(const std::string& user, const std::string& followee) {
        auto r = httpPost(base_url+"/wrk2-api/user/follow",
            {{"user_name",user},{"followee_name",followee}});
        return r.status==200 && r.body.find("Success")!=std::string::npos;
    }

    bool unfollowByName(const std::string& user, const std::string& followee) {
        auto r = httpPost(base_url+"/wrk2-api/user/unfollow",
            {{"user_name",user},{"followee_name",followee}});
        return r.status==200;
    }

    HttpResponse composePost(const std::string& username, int user_id, const std::string& text) {
        return httpPost(base_url+"/wrk2-api/post/compose",
            {{"username",username},{"user_id",std::to_string(user_id)},
             {"text",text},{"media_ids","[]"},{"media_types","[]"},{"post_type","0"}});
    }

    json readUserTimeline(int user_id, int start=0, int stop=50) {
        auto r = httpGet(base_url+"/wrk2-api/user-timeline/read?user_id="+
            std::to_string(user_id)+"&start="+std::to_string(start)+"&stop="+std::to_string(stop));
        if (r.status!=200) return json::array();
        try {
            auto j = json::parse(r.body);
            if (j.is_object()&&j.empty()) return json::array();
            return j;
        } catch(...) { return json::array(); }
    }

    json readHomeTimeline(int user_id, int start=0, int stop=50) {
        auto r = httpGet(base_url+"/wrk2-api/home-timeline/read?user_id="+
            std::to_string(user_id)+"&start="+std::to_string(start)+"&stop="+std::to_string(stop));
        if (r.status!=200) return json::array();
        try {
            auto j = json::parse(r.body);
            if (j.is_object()&&j.empty()) return json::array();
            return j;
        } catch(...) { return json::array(); }
    }

    std::string findLatestPostId(int user_id) {
        auto tl = readUserTimeline(user_id, 0, 1);
        if (tl.is_array()&&!tl.empty()&&tl[0].contains("post_id"))
            return tl[0]["post_id"].get<std::string>();
        return "";
    }

    // check-api: does NOT query DB directly — goes through Thrift service layer
    bool checkUserTimelineContains(int user_id, const std::string& post_id) {
        auto r = httpGet(base_url+"/check-api/user-timeline/contains?user_id="+
            std::to_string(user_id)+"&post_id="+urlEncode(post_id));
        if (r.status!=200) return false;
        try { auto j=json::parse(r.body); return j.value("found",false); } catch(...) { return false; }
    }

    bool checkHomeTimelineContains(int user_id, const std::string& post_id) {
        auto r = httpGet(base_url+"/check-api/home-timeline/contains?user_id="+
            std::to_string(user_id)+"&post_id="+urlEncode(post_id));
        if (r.status!=200) return false;
        try { auto j=json::parse(r.body); return j.value("found",false); } catch(...) { return false; }
    }

    bool pollHomeTimelineContains(int user_id, const std::string& post_id,
                                   int timeout_ms=5000, int poll_ms=100) {
        auto deadline = std::chrono::steady_clock::now()+std::chrono::milliseconds(timeout_ms);
        while (std::chrono::steady_clock::now()<deadline) {
            if (checkHomeTimelineContains(user_id, post_id)) return true;
            std::this_thread::sleep_for(std::chrono::milliseconds(poll_ms));
        }
        return false;
    }

    std::vector<std::string> getFollowees(int user_id) {
        std::vector<std::string> result;
        auto r = httpGet(base_url+"/check-api/social-graph/get_followees?user_id="+std::to_string(user_id));
        if (r.status!=200) return result;
        try {
            auto arr=json::parse(r.body);
            if (arr.is_array()) for (auto& v:arr) result.push_back(v.get<std::string>());
        } catch(...) {}
        return result;
    }

    bool timelineContainsPost(const json& tl, const std::string& post_id) {
        if (!tl.is_array()) return false;
        for (auto& p:tl) if (p.contains("post_id")&&p["post_id"].get<std::string>()==post_id) return true;
        return false;
    }
};
