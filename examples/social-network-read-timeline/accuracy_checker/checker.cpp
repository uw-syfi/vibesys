#include <iostream>
#include <string>
#include <vector>
#include <map>
#include <set>
#include <algorithm>
#include <atomic>
#include <thread>
#include <chrono>
#include <functional>
#include <curl/curl.h>
#include "../include/social_network_client.h"

static std::string BASE_URL = "http://localhost:8080";
static int ASYNC_TIMEOUT_MS = 5000;
static int POLL_INTERVAL_MS = 100;
static std::atomic<int> USER_ID_COUNTER{0};

static int nextUserId() { return USER_ID_COUNTER.fetch_add(1); }

struct TestResult {
    std::string name;
    bool passed = false;
    std::string detail;
};

struct SetupData {
    bool valid = false;
    std::string error;
    std::vector<int> user_ids;
    std::vector<std::string> usernames;
    std::vector<std::pair<std::string,std::string>> follows;
};

static bool validatePostSchema(const json& post, std::string& err) {
    for (auto& f : {"post_id","creator","text","timestamp","post_type","user_mentions","media","urls"}) {
        if (!post.contains(f)) { err = std::string("missing field: ")+f; return false; }
    }
    if (!post["creator"].contains("user_id")||!post["creator"].contains("username")) {
        err = "creator missing fields"; return false;
    }
    if (post["post_id"].get<std::string>().empty()) { err = "empty post_id"; return false; }
    if (post["timestamp"].get<std::string>().empty()) { err = "empty timestamp"; return false; }
    return true;
}

static std::string registerAndCompose(SocialNetworkClient& c, const std::string& uname,
                                       int uid, const std::string& text,
                                       SetupData& s, bool add_follow_seed=false) {
    if (!c.registerUser(uname, "pass123", uid, "RT", std::to_string(uid))) {
        s.error = "register failed for "+uname; return "";
    }
    s.user_ids.push_back(uid);
    s.usernames.push_back(uname);
    auto r = c.composePost(uname, uid, text);
    bool ok = r.status==200 || (r.status==500 && r.body.find("ZADD")!=std::string::npos);
    if (!ok) { s.error = "compose failed: "+r.body; return ""; }
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    return c.findLatestPostId(uid);
}

static void Reset(SocialNetworkClient& c, const SetupData& s) {
    for (auto& f : s.follows) c.unfollowByName(f.first, f.second);
}

SetupData Setup_C1(SocialNetworkClient& c) {
    SetupData s;
    int uid = nextUserId();
    std::string uname = "rchk_c1_"+std::to_string(uid);
    auto pid = registerAndCompose(c, uname, uid, "C1_schema_test", s);
    if (!s.error.empty()) return s;
    if (pid.empty()) { s.error = "could not get post_id"; return s; }
    s.valid = true;
    return s;
}

TestResult Test_C1() {
    TestResult res{"C1 User-timeline response shape compatibility", false, ""};
    SocialNetworkClient c(BASE_URL);
    auto s = Setup_C1(c);
    if (!s.valid) { res.detail = "setup: "+s.error; Reset(c,s); return res; }

    auto tl = c.readUserTimeline(s.user_ids[0], 0, 5);
    if (!tl.is_array()||tl.empty()) { res.detail = "timeline empty or not array"; Reset(c,s); return res; }
    std::string err;
    if (!validatePostSchema(tl[0], err)) { res.detail = "schema: "+err; Reset(c,s); return res; }

    Reset(c,s);
    res.passed = true;
    return res;
}

SetupData Setup_C2(SocialNetworkClient& c) {
    SetupData s;
    int uid_a = nextUserId(), uid_b = nextUserId();
    std::string ua = "rchk_c2a_"+std::to_string(uid_a);
    std::string ub = "rchk_c2b_"+std::to_string(uid_b);
    if (!c.registerUser(ua,"pass123",uid_a,"RT","C2A")||!c.registerUser(ub,"pass123",uid_b,"RT","C2B")) {
        s.error="register failed"; return s;
    }
    s.user_ids = {uid_a, uid_b};
    s.usernames = {ua, ub};
    if (!c.followByName(ua, ub)) { s.error="follow failed"; return s; }
    s.follows.push_back({ua,ub});
    auto r = c.composePost(ub, uid_b, "C2_ht_schema");
    bool ok = r.status==200||(r.status==500&&r.body.find("ZADD")!=std::string::npos);
    if (!ok) { s.error="compose failed: "+r.body; return s; }
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    s.valid = true;
    return s;
}

TestResult Test_C2() {
    TestResult res{"C2 Home-timeline response shape compatibility", false, ""};
    SocialNetworkClient c(BASE_URL);
    auto s = Setup_C2(c);
    if (!s.valid) { res.detail = "setup: "+s.error; Reset(c,s); return res; }

    auto htl = c.readHomeTimeline(s.user_ids[0], 0, 5);
    if (!htl.is_array()||htl.empty()) { res.detail = "home-timeline empty or not array"; Reset(c,s); return res; }
    std::string err;
    if (!validatePostSchema(htl[0], err)) { res.detail = "schema: "+err; Reset(c,s); return res; }

    Reset(c,s);
    res.passed = true;
    return res;
}

SetupData Setup_C3(SocialNetworkClient& c) {
    SetupData s;
    int uid = nextUserId();
    std::string uname = "rchk_c3_"+std::to_string(uid);
    if (!c.registerUser(uname,"pass123",uid,"RT","C3")) { s.error="register failed"; return s; }
    s.user_ids={uid}; s.usernames={uname};
    for (int i=1; i<=3; i++) {
        auto r = c.composePost(uname, uid, "C3_post_"+std::to_string(i));
        bool ok = r.status==200||(r.status==500&&r.body.find("ZADD")!=std::string::npos);
        if (!ok) { s.error="compose "+std::to_string(i)+" failed"; return s; }
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(150));
    s.valid = true;
    return s;
}

TestResult Test_C3() {
    TestResult res{"C3 User-timeline ordering is descending by timestamp", false, ""};
    SocialNetworkClient c(BASE_URL);
    auto s = Setup_C3(c);
    if (!s.valid) { res.detail = "setup: "+s.error; Reset(c,s); return res; }

    auto tl = c.readUserTimeline(s.user_ids[0], 0, 10);
    if (!tl.is_array()||(int)tl.size()<3) {
        res.detail = "expected >=3 posts, got "+std::to_string(tl.is_array()?tl.size():0);
        Reset(c,s); return res;
    }
    long long prev = LLONG_MAX;
    for (int i=0; i<3; i++) {
        long long ts = std::stoll(tl[i]["timestamp"].get<std::string>());
        if (ts > prev) {
            res.detail = "ordering violated at index "+std::to_string(i);
            Reset(c,s); return res;
        }
        prev = ts;
    }

    Reset(c,s);
    res.passed = true;
    return res;
}

SetupData Setup_C4(SocialNetworkClient& c) {
    SetupData s;
    int uid_a=nextUserId(), uid_b=nextUserId();
    std::string ua="rchk_c4a_"+std::to_string(uid_a);
    std::string ub="rchk_c4b_"+std::to_string(uid_b);
    if (!c.registerUser(ua,"pass123",uid_a,"RT","C4A")||!c.registerUser(ub,"pass123",uid_b,"RT","C4B")) {
        s.error="register failed"; return s;
    }
    s.user_ids={uid_a,uid_b}; s.usernames={ua,ub};
    if (!c.followByName(ua,ub)) { s.error="follow failed"; return s; }
    s.follows.push_back({ua,ub});
    for (int i=1; i<=3; i++) {
        auto r = c.composePost(ub, uid_b, "C4_ht_post_"+std::to_string(i));
        bool ok = r.status==200||(r.status==500&&r.body.find("ZADD")!=std::string::npos);
        if (!ok) { s.error="compose failed"; return s; }
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    s.valid = true;
    return s;
}

TestResult Test_C4() {
    TestResult res{"C4 Home-timeline ordering is descending by timestamp", false, ""};
    SocialNetworkClient c(BASE_URL);
    auto s = Setup_C4(c);
    if (!s.valid) { res.detail = "setup: "+s.error; Reset(c,s); return res; }

    auto htl = c.readHomeTimeline(s.user_ids[0], 0, 10);
    if (!htl.is_array()||(int)htl.size()<3) {
        res.detail = "expected >=3 posts in home-timeline, got "+std::to_string(htl.is_array()?htl.size():0);
        Reset(c,s); return res;
    }
    long long prev = LLONG_MAX;
    for (int i=0; i<3; i++) {
        long long ts = std::stoll(htl[i]["timestamp"].get<std::string>());
        if (ts > prev) { res.detail = "ht ordering violated at index "+std::to_string(i); Reset(c,s); return res; }
        prev = ts;
    }

    Reset(c,s);
    res.passed = true;
    return res;
}

SetupData Setup_C5(SocialNetworkClient& c) {
    SetupData s;
    int uid_a=nextUserId(), uid_b=nextUserId(), uid_c=nextUserId();
    std::string ua="rchk_c5a_"+std::to_string(uid_a);
    std::string ub="rchk_c5b_"+std::to_string(uid_b);
    std::string uc="rchk_c5c_"+std::to_string(uid_c);
    if (!c.registerUser(ua,"pass123",uid_a,"RT","C5A")||
        !c.registerUser(ub,"pass123",uid_b,"RT","C5B")||
        !c.registerUser(uc,"pass123",uid_c,"RT","C5C")) {
        s.error="register failed"; return s;
    }
    s.user_ids={uid_a,uid_b,uid_c}; s.usernames={ua,ub,uc};
    if (!c.followByName(ua,ub)) { s.error="follow a->b failed"; return s; }
    s.follows.push_back({ua,ub});
    s.valid = true;
    return s;
}

TestResult Test_C5() {
    TestResult res{"C5 Visibility rules: home-timeline only shows followed users posts", false, ""};
    SocialNetworkClient c(BASE_URL);
    auto s = Setup_C5(c);
    if (!s.valid) { res.detail = "setup: "+s.error; Reset(c,s); return res; }

    int uid_a=s.user_ids[0], uid_b=s.user_ids[1], uid_c=s.user_ids[2];

    auto rb = c.composePost(s.usernames[1], uid_b, "C5_from_followed");
    bool ok_b = rb.status==200||(rb.status==500&&rb.body.find("ZADD")!=std::string::npos);
    auto rc = c.composePost(s.usernames[2], uid_c, "C5_from_not_followed");
    bool ok_c = rc.status==200||(rc.status==500&&rc.body.find("ZADD")!=std::string::npos);

    if (!ok_b||!ok_c) { res.detail="compose failed"; Reset(c,s); return res; }
    std::this_thread::sleep_for(std::chrono::milliseconds(100));

    std::string b_pid = c.findLatestPostId(uid_b);
    std::string c_pid = c.findLatestPostId(uid_c);
    if (b_pid.empty()||c_pid.empty()) { res.detail="could not get post_ids"; Reset(c,s); return res; }

    bool b_found = c.pollHomeTimelineContains(uid_a, b_pid, ASYNC_TIMEOUT_MS, POLL_INTERVAL_MS);
    if (!b_found) { res.detail="post from followed user not in home-timeline"; Reset(c,s); return res; }

    std::this_thread::sleep_for(std::chrono::milliseconds(1000));
    bool c_found = c.checkHomeTimelineContains(uid_a, c_pid);
    if (c_found) { res.detail="post from non-followed user appeared in home-timeline"; Reset(c,s); return res; }

    Reset(c,s);
    res.passed = true;
    return res;
}

SetupData Setup_C6(SocialNetworkClient& c) {
    SetupData s;
    int uid = nextUserId();
    std::string uname = "rchk_c6_"+std::to_string(uid);
    if (!c.registerUser(uname,"pass123",uid,"RT","C6")) { s.error="register failed"; return s; }
    s.user_ids={uid}; s.usernames={uname};
    s.valid = true;
    return s;
}

TestResult Test_C6() {
    TestResult res{"C6 Write-then-read consistency: compose reflects in user-timeline", false, ""};
    SocialNetworkClient c(BASE_URL);
    auto s = Setup_C6(c);
    if (!s.valid) { res.detail = "setup: "+s.error; Reset(c,s); return res; }

    std::string marker = "C6_consistency_"+std::to_string(s.user_ids[0]);
    auto r = c.composePost(s.usernames[0], s.user_ids[0], marker);
    bool ok = r.status==200||(r.status==500&&r.body.find("ZADD")!=std::string::npos);
    if (!ok) { res.detail="compose failed: "+r.body; Reset(c,s); return res; }
    std::this_thread::sleep_for(std::chrono::milliseconds(100));

    std::string post_id = c.findLatestPostId(s.user_ids[0]);
    if (post_id.empty()) { res.detail="findLatestPostId returned empty"; Reset(c,s); return res; }

    bool found = c.checkUserTimelineContains(s.user_ids[0], post_id);
    if (!found) { res.detail="post not found in user-timeline via check-api"; Reset(c,s); return res; }

    auto tl = c.readUserTimeline(s.user_ids[0], 0, 3);
    if (!tl.is_array()||tl.empty()) { res.detail="timeline empty after compose"; Reset(c,s); return res; }
    if (tl[0]["text"].get<std::string>() != marker) { res.detail="text mismatch in timeline"; Reset(c,s); return res; }

    Reset(c,s);
    res.passed = true;
    return res;
}

SetupData Setup_C7(SocialNetworkClient& c) {
    SetupData s;
    int uid_a=nextUserId(), uid_b=nextUserId();
    std::string ua="rchk_c7a_"+std::to_string(uid_a);
    std::string ub="rchk_c7b_"+std::to_string(uid_b);
    if (!c.registerUser(ua,"pass123",uid_a,"RT","C7A")||!c.registerUser(ub,"pass123",uid_b,"RT","C7B")) {
        s.error="register failed"; return s;
    }
    s.user_ids={uid_a,uid_b}; s.usernames={ua,ub};
    if (!c.followByName(ua,ub)) { s.error="follow failed"; return s; }
    s.follows.push_back({ua,ub});
    s.valid = true;
    return s;
}

TestResult Test_C7() {
    TestResult res{"C7 Write-then-read consistency: compose fans out to follower home-timeline", false, ""};
    SocialNetworkClient c(BASE_URL);
    auto s = Setup_C7(c);
    if (!s.valid) { res.detail = "setup: "+s.error; Reset(c,s); return res; }

    int uid_a=s.user_ids[0], uid_b=s.user_ids[1];
    auto r = c.composePost(s.usernames[1], uid_b, "C7_fanout_test");
    bool ok = r.status==200||(r.status==500&&r.body.find("ZADD")!=std::string::npos);
    if (!ok) { res.detail="compose failed"; Reset(c,s); return res; }
    std::this_thread::sleep_for(std::chrono::milliseconds(100));

    std::string post_id = c.findLatestPostId(uid_b);
    if (post_id.empty()) { res.detail="could not get post_id"; Reset(c,s); return res; }

    bool found = c.pollHomeTimelineContains(uid_a, post_id, ASYNC_TIMEOUT_MS, POLL_INTERVAL_MS);
    if (!found) {
        res.detail = "post not in follower home-timeline after "+std::to_string(ASYNC_TIMEOUT_MS)+"ms";
        Reset(c,s); return res;
    }

    Reset(c,s);
    res.passed = true;
    return res;
}

SetupData Setup_C8(SocialNetworkClient& c) {
    SetupData s;
    int uid_a=nextUserId(), uid_b=nextUserId();
    std::string ua="rchk_c8a_"+std::to_string(uid_a);
    std::string ub="rchk_c8b_"+std::to_string(uid_b);
    if (!c.registerUser(ua,"pass123",uid_a,"RT","C8A")||!c.registerUser(ub,"pass123",uid_b,"RT","C8B")) {
        s.error="register failed"; return s;
    }
    s.user_ids={uid_a,uid_b}; s.usernames={ua,ub};
    if (!c.followByName(ua,ub)) { s.error="follow failed"; return s; }
    s.follows.push_back({ua,ub});
    auto r = c.composePost(ub, uid_b, "C8_pre_unfollow");
    bool ok = r.status==200||(r.status==500&&r.body.find("ZADD")!=std::string::npos);
    if (!ok) { s.error="compose failed"; return s; }
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    std::string pid = c.findLatestPostId(uid_b);
    if (!c.pollHomeTimelineContains(uid_a, pid, 3000, 100)) {
        s.error="pre-unfollow post never appeared (fan-out broken)"; return s;
    }
    s.valid = true;
    return s;
}

TestResult Test_C8() {
    TestResult res{"C8 Unfollow stops future fan-out to home-timeline", false, ""};
    SocialNetworkClient c(BASE_URL);
    auto s = Setup_C8(c);
    if (!s.valid) { res.detail = "setup: "+s.error; Reset(c,s); return res; }

    int uid_a=s.user_ids[0], uid_b=s.user_ids[1];
    if (!c.unfollowByName(s.usernames[0], s.usernames[1])) {
        res.detail="unfollow call failed"; Reset(c,s); return res;
    }
    s.follows.clear();
    std::this_thread::sleep_for(std::chrono::milliseconds(200));

    auto r = c.composePost(s.usernames[1], uid_b, "C8_post_unfollow");
    bool ok = r.status==200||(r.status==500&&r.body.find("ZADD")!=std::string::npos);
    if (!ok) { res.detail="post-unfollow compose failed"; Reset(c,s); return res; }
    std::this_thread::sleep_for(std::chrono::milliseconds(100));

    std::string post_id = c.findLatestPostId(uid_b);
    if (post_id.empty()) { res.detail="could not get post_id"; Reset(c,s); return res; }

    std::this_thread::sleep_for(std::chrono::milliseconds(2000));
    bool found = c.checkHomeTimelineContains(uid_a, post_id);
    if (found) { res.detail="post after unfollow appeared in home-timeline"; Reset(c,s); return res; }

    Reset(c,s);
    res.passed = true;
    return res;
}

SetupData Setup_C9(SocialNetworkClient& c) {
    SetupData s;
    int uid = nextUserId();
    std::string uname = "rchk_c9_"+std::to_string(uid);
    auto pid = registerAndCompose(c, uname, uid, "C9_baseline", s);
    if (!s.error.empty()) return s;
    if (pid.empty()) { s.error="compose baseline failed"; return s; }
    s.valid = true;
    return s;
}

TestResult Test_C9() {
    TestResult res{"C9 Failed reads do not mutate application state", false, ""};
    SocialNetworkClient c(BASE_URL);
    auto s = Setup_C9(c);
    if (!s.valid) { res.detail = "setup: "+s.error; Reset(c,s); return res; }

    auto before = c.readUserTimeline(s.user_ids[0], 0, 50);
    size_t count_before = before.is_array() ? before.size() : 0;

    int invalid_uid = 999999999;
    auto r1 = httpGet(BASE_URL+"/wrk2-api/user-timeline/read?user_id="+std::to_string(invalid_uid)+"&start=0&stop=10");
    auto r2 = httpGet(BASE_URL+"/wrk2-api/home-timeline/read?user_id="+std::to_string(invalid_uid)+"&start=0&stop=10");

    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    auto after = c.readUserTimeline(s.user_ids[0], 0, 50);
    size_t count_after = after.is_array() ? after.size() : 0;

    if (count_after != count_before) {
        res.detail = "timeline count changed from "+std::to_string(count_before)+
                     " to "+std::to_string(count_after)+" after invalid reads";
        Reset(c,s); return res;
    }

    Reset(c,s);
    res.passed = true;
    return res;
}

SetupData Setup_C10(SocialNetworkClient& c) {
    SetupData s;
    int uid = nextUserId();
    std::string uname = "rchk_c10_"+std::to_string(uid);
    if (!c.registerUser(uname,"pass123",uid,"RT","C10")) { s.error="register failed"; return s; }
    s.user_ids={uid}; s.usernames={uname};
    for (int i=0; i<6; i++) {
        auto r = c.composePost(uname, uid, "C10_page_post_"+std::to_string(i));
        bool ok = r.status==200||(r.status==500&&r.body.find("ZADD")!=std::string::npos);
        if (!ok) { s.error="compose "+std::to_string(i)+" failed"; return s; }
        std::this_thread::sleep_for(std::chrono::milliseconds(30));
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    s.valid = true;
    return s;
}

TestResult Test_C10() {
    TestResult res{"C10 Pagination: start/stop bounds return disjoint non-overlapping pages", false, ""};
    SocialNetworkClient c(BASE_URL);
    auto s = Setup_C10(c);
    if (!s.valid) { res.detail = "setup: "+s.error; Reset(c,s); return res; }

    auto page1 = c.readUserTimeline(s.user_ids[0], 0, 3);
    auto page2 = c.readUserTimeline(s.user_ids[0], 3, 6);

    if (!page1.is_array()||(int)page1.size()!=3) {
        res.detail="page1 expected 3, got "+std::to_string(page1.is_array()?page1.size():0);
        Reset(c,s); return res;
    }
    if (!page2.is_array()||(int)page2.size()!=3) {
        res.detail="page2 expected 3, got "+std::to_string(page2.is_array()?page2.size():0);
        Reset(c,s); return res;
    }

    std::set<std::string> ids1, ids2;
    for (auto& p:page1) ids1.insert(p["post_id"].get<std::string>());
    for (auto& p:page2) ids2.insert(p["post_id"].get<std::string>());

    std::vector<std::string> overlap;
    for (auto& id:ids1) if (ids2.count(id)) overlap.push_back(id);
    if (!overlap.empty()) { res.detail="overlap between pages: "+overlap[0]; Reset(c,s); return res; }

    Reset(c,s);
    res.passed = true;
    return res;
}

SetupData Setup_C11(SocialNetworkClient& c) {
    SetupData s;
    int uid_a=nextUserId(), uid_b=nextUserId(), uid_c=nextUserId();
    std::string ua="rchk_c11a_"+std::to_string(uid_a);
    std::string ub="rchk_c11b_"+std::to_string(uid_b);
    std::string uc="rchk_c11c_"+std::to_string(uid_c);
    if (!c.registerUser(ua,"pass123",uid_a,"RT","C11A")||
        !c.registerUser(ub,"pass123",uid_b,"RT","C11B")||
        !c.registerUser(uc,"pass123",uid_c,"RT","C11C")) {
        s.error="register failed"; return s;
    }
    s.user_ids={uid_a,uid_b,uid_c}; s.usernames={ua,ub,uc};
    if (!c.followByName(ua,ub)||!c.followByName(ub,uc)) { s.error="follow failed"; return s; }
    s.follows.push_back({ua,ub}); s.follows.push_back({ub,uc});
    for (int i=0; i<5; i++) {
        auto r = c.composePost(ub, uid_b, "C11_burst_post_"+std::to_string(i));
        bool ok = r.status==200||(r.status==500&&r.body.find("ZADD")!=std::string::npos);
        if (!ok) { s.error="compose failed"; return s; }
        std::this_thread::sleep_for(std::chrono::milliseconds(30));
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    s.valid = true;
    return s;
}

TestResult Test_C11() {
    TestResult res{"C11 Held-out sequences: burst-read consistency and cross-user visibility", false, ""};
    SocialNetworkClient c(BASE_URL);
    auto s = Setup_C11(c);
    if (!s.valid) { res.detail = "setup: "+s.error; Reset(c,s); return res; }

    int uid_a=s.user_ids[0], uid_b=s.user_ids[1], uid_c=s.user_ids[2];

    // Sequence 1: burst read — read same timeline 8 times, all must return identical post_ids
    std::vector<std::set<std::string>> readings;
    for (int i=0; i<8; i++) {
        auto tl = c.readUserTimeline(uid_b, 0, 10);
        std::set<std::string> ids;
        if (tl.is_array()) for (auto& p:tl) ids.insert(p["post_id"].get<std::string>());
        readings.push_back(ids);
    }
    for (size_t i=1; i<readings.size(); i++) {
        if (readings[i]!=readings[0]) {
            res.detail="burst-read inconsistency at iteration "+std::to_string(i);
            Reset(c,s); return res;
        }
    }

    // Sequence 2: check-api visibility — ua follows ub, uc does not follow ub
    // ub has posts; ua should see them in home-timeline, uc should not
    std::string ub_pid = c.findLatestPostId(uid_b);
    if (ub_pid.empty()) { res.detail="could not get ub post_id"; Reset(c,s); return res; }

    bool ua_sees = c.checkHomeTimelineContains(uid_a, ub_pid);
    bool uc_sees = c.checkHomeTimelineContains(uid_c, ub_pid);
    if (!ua_sees) { res.detail="ua (follower of ub) does not see ub's post in home-timeline"; Reset(c,s); return res; }
    if (uc_sees) { res.detail="uc (not follower of ub) sees ub's post in home-timeline"; Reset(c,s); return res; }

    // Sequence 3: page boundary consistency — sequential pages together equal full read
    auto full = c.readUserTimeline(uid_b, 0, 5);
    auto p1   = c.readUserTimeline(uid_b, 0, 2);
    auto p2   = c.readUserTimeline(uid_b, 2, 5);

    std::set<std::string> full_ids, paged_ids;
    if (full.is_array()) for (auto& p:full) full_ids.insert(p["post_id"].get<std::string>());
    if (p1.is_array()) for (auto& p:p1)   paged_ids.insert(p["post_id"].get<std::string>());
    if (p2.is_array()) for (auto& p:p2)   paged_ids.insert(p["post_id"].get<std::string>());

    if (full_ids != paged_ids) {
        res.detail = "paginated read does not match full read (different post sets)";
        Reset(c,s); return res;
    }

    Reset(c,s);
    res.passed = true;
    return res;
}

int main(int argc, char* argv[]) {
    for (int i=1; i<argc; i++) {
        std::string a=argv[i];
        if (a=="--base-url"&&i+1<argc) BASE_URL=argv[++i];
        else if (a=="--timeout-ms"&&i+1<argc) ASYNC_TIMEOUT_MS=std::stoi(argv[++i]);
        else if (a=="--poll-ms"&&i+1<argc) POLL_INTERVAL_MS=std::stoi(argv[++i]);
    }

    curl_global_init(CURL_GLOBAL_ALL);

    USER_ID_COUNTER = 600000 + (int)(
        std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::system_clock::now().time_since_epoch()).count() % 90000);

    auto probe = httpGet(BASE_URL+"/wrk2-api/user-timeline/read?user_id=1&start=0&stop=1");
    if (probe.status==0) {
        std::cerr<<"ERROR: cannot reach "<<BASE_URL<<"\n";
        curl_global_cleanup(); return 1;
    }

    std::vector<std::function<TestResult()>> tests = {
        Test_C1, Test_C2, Test_C3, Test_C4, Test_C5, Test_C6,
        Test_C7, Test_C8, Test_C9, Test_C10, Test_C11
    };

    int passed=0, failed=0;
    std::vector<std::pair<std::string,std::string>> failures;

    for (auto& t:tests) {
        auto r=t();
        if (r.passed) { std::cout<<"  PASS  "<<r.name<<"\n"; passed++; }
        else { std::cout<<"  FAIL  "<<r.name<<": "<<r.detail<<"\n"; failed++; failures.push_back({r.name,r.detail}); }
        std::cout.flush();
    }

    std::cout<<"\nResults: "<<passed<<" passed, "<<failed<<" failed out of "<<(passed+failed)<<" checks.\n";
    if (!failures.empty()) {
        std::cout<<"\nFailed checks:\n";
        for (auto& f:failures) std::cout<<"  - "<<f.first<<": "<<f.second<<"\n";
    }

    curl_global_cleanup();
    return failed>0?1:0;
}
