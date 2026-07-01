#include <iostream>
#include <string>
#include <vector>
#include <map>
#include <thread>
#include <mutex>
#include <atomic>
#include <chrono>
#include <queue>
#include <condition_variable>
#include <algorithm>
#include <numeric>
#include <sstream>
#include <iomanip>
#include <random>
#include <functional>
#include <cstdio>
#include <curl/curl.h>
#include "../include/social_network_client.h"
#include "../include/percentile.h"

static std::string BASE_URL  = "http://localhost:8080";
static std::string LOAD_LEVEL = "medium";
static int  SEED        = 42;
static bool SKIP_SETUP  = false;

struct LoadConfig {
    int target_rps;
    int duration_sec;
    int warmup_sec;
    int n_users;
    int seed_posts_per_user;
};

static const std::map<std::string,LoadConfig> LOAD_LEVELS = {
    {"light",  {100,  60,  10, 20,  5}},
    {"medium", {300,  120, 20, 50,  10}},
    {"heavy",  {600,  180, 30, 100, 10}},
};

enum class ReqType { USER_TIMELINE, HOME_TIMELINE, COMPOSE };

struct Measurement {
    double  latency_ms;
    double  thrift_ms;
    int     status;
    ReqType type;
};

static std::atomic<bool>  g_stop{false};
static std::mutex         g_meas_mutex;
static std::vector<Measurement> g_measurements;

static void emit(const std::string& k, double v) {
    std::cout<<"{\"metric\":\""<<k<<"\",\"value\":"<<std::fixed<<std::setprecision(2)<<v<<"}\n";
    std::cout.flush();
}

static std::string rbnchUser(int i)  { return "rbnch_"+std::to_string(i); }
static int         rbnchId(int i)    { return 700000+i; }

static void setupUsers(const LoadConfig& cfg) {
    std::cout<<"# Setting up "<<cfg.n_users<<" read-timeline benchmark users...\n"; std::cout.flush();
    SocialNetworkClient c(BASE_URL);
    for (int i=0; i<cfg.n_users; i++) {
        c.registerUser(rbnchUser(i),"rbnch_pass",rbnchId(i),"RB",std::to_string(i));
        if (i>0) c.followByName(rbnchUser(i), rbnchUser(i-1));
    }
    if (cfg.n_users>1) c.followByName(rbnchUser(0), rbnchUser(cfg.n_users-1));
    std::cout<<"# Seeding "<<cfg.seed_posts_per_user<<" posts per user...\n"; std::cout.flush();
    for (int i=0; i<cfg.n_users; i++)
        for (int j=0; j<cfg.seed_posts_per_user; j++)
            c.composePost(rbnchUser(i), rbnchId(i), "seed_"+std::to_string(j));
    std::this_thread::sleep_for(std::chrono::seconds(3));
    std::cout<<"# Setup complete.\n"; std::cout.flush();
}

static Measurement doUserTimelineRead(const std::string& base_url, int user_id) {
    Measurement m; m.type=ReqType::USER_TIMELINE; m.thrift_ms=0;
    auto r = httpGet(base_url+"/wrk2-api/user-timeline/read?user_id="+std::to_string(user_id)+"&start=0&stop=10");
    m.latency_ms=r.latency_ms; m.status=r.status;
    auto it=r.headers.find("X-UserTimeline-Thrift-Ms");
    if (it!=r.headers.end()) try{m.thrift_ms=std::stod(it->second);}catch(...){}
    return m;
}

static Measurement doHomeTimelineRead(const std::string& base_url, int user_id) {
    Measurement m; m.type=ReqType::HOME_TIMELINE; m.thrift_ms=0;
    auto r = httpGet(base_url+"/wrk2-api/home-timeline/read?user_id="+std::to_string(user_id)+"&start=0&stop=10");
    m.latency_ms=r.latency_ms; m.status=r.status;
    auto it=r.headers.find("X-HomeTimeline-Thrift-Ms");
    if (it!=r.headers.end()) try{m.thrift_ms=std::stod(it->second);}catch(...){}
    return m;
}

static Measurement doCompose(const std::string& base_url, const std::string& uname, int uid, int ctr) {
    Measurement m; m.type=ReqType::COMPOSE; m.thrift_ms=0;
    auto r = httpPost(base_url+"/wrk2-api/post/compose",
        {{"username",uname},{"user_id",std::to_string(uid)},
         {"text","live_"+std::to_string(ctr)},
         {"media_ids","[]"},{"media_types","[]"},{"post_type","0"}});
    m.latency_ms=r.latency_ms; m.status=r.status;
    auto it=r.headers.find("X-Compose-Thrift-Ms");
    if (it!=r.headers.end()) try{m.thrift_ms=std::stod(it->second);}catch(...){}
    return m;
}

struct WorkItem { ReqType type; int user_idx; int counter; };

static std::queue<WorkItem>       g_work_queue;
static std::mutex                 g_queue_mutex;
static std::condition_variable    g_queue_cv;
static std::atomic<bool>          g_producer_done{false};

static void workerThread(const std::string& base_url, int n_users) {
    while (true) {
        WorkItem item;
        {
            std::unique_lock<std::mutex> lk(g_queue_mutex);
            g_queue_cv.wait(lk,[]{return !g_work_queue.empty()||g_producer_done.load();});
            if (g_work_queue.empty()&&g_producer_done.load()) break;
            if (g_work_queue.empty()) continue;
            item=g_work_queue.front(); g_work_queue.pop();
        }
        if (g_stop.load()) continue;
        Measurement m;
        if (item.type==ReqType::USER_TIMELINE) m=doUserTimelineRead(base_url, rbnchId(item.user_idx));
        else if (item.type==ReqType::HOME_TIMELINE) m=doHomeTimelineRead(base_url, rbnchId(item.user_idx));
        else m=doCompose(base_url, rbnchUser(item.user_idx), rbnchId(item.user_idx), item.counter);
        {
            std::lock_guard<std::mutex> lk(g_meas_mutex);
            g_measurements.push_back(m);
        }
    }
}

// Open-loop token bucket: 50% user-timeline, 40% home-timeline, 10% compose
static void tokenBucketProducer(const LoadConfig& cfg, bool warmup, std::mt19937& rng) {
    int duration = warmup ? cfg.warmup_sec : cfg.duration_sec;
    double interval_us = 1000000.0 / cfg.target_rps;
    auto deadline = std::chrono::steady_clock::now()+std::chrono::seconds(duration);
    auto next_issue = std::chrono::steady_clock::now();
    std::uniform_int_distribution<int> user_dist(0, cfg.n_users-1);
    std::uniform_real_distribution<double> type_dist(0.0,1.0);
    int counter=0;
    while (std::chrono::steady_clock::now()<deadline) {
        auto now=std::chrono::steady_clock::now();
        if (now<next_issue) std::this_thread::sleep_for(next_issue-now);
        next_issue+=std::chrono::microseconds((long long)interval_us);
        WorkItem item;
        item.user_idx=user_dist(rng);
        item.counter=counter++;
        double r=type_dist(rng);
        if      (r<0.50) item.type=ReqType::USER_TIMELINE;
        else if (r<0.90) item.type=ReqType::HOME_TIMELINE;
        else             item.type=ReqType::COMPOSE;
        {std::lock_guard<std::mutex> lk(g_queue_mutex); g_work_queue.push(item);}
        g_queue_cv.notify_one();
    }
}

static std::pair<double,double> dockerStats() {
    FILE* p=popen("docker stats --no-stream --format \"{{json .}}\" 2>/dev/null","r");
    if (!p) return {0,0};
    double cpu=0,mem=0; char buf[4096];
    while (fgets(buf,sizeof(buf),p)) {
        std::string line(buf);
        if (line.find("socialnetwork")==std::string::npos) continue;
        try {
            auto j=json::parse(line);
            std::string cs=j.value("CPUPerc","0%");
            cs.erase(std::remove(cs.begin(),cs.end(),'%'),cs.end());
            cpu+=std::stod(cs);
            std::string ms=j.value("MemUsage","0B / 0B").substr(0,j.value("MemUsage","0B / 0B").find('/'));
            ms.erase(0,ms.find_first_not_of(" \t")); ms.erase(ms.find_last_not_of(" \t\r\n")+1);
            double mb=0;
            if (ms.find("GiB")!=std::string::npos) mb=std::stod(ms)*1024;
            else if (ms.find("MiB")!=std::string::npos) mb=std::stod(ms);
            else if (ms.find("KiB")!=std::string::npos) mb=std::stod(ms)/1024;
            mem+=mb;
        } catch(...) {}
    }
    pclose(p); return {cpu,mem};
}

static void dockerPoller(std::vector<std::pair<double,double>>& samples, std::atomic<bool>& stop) {
    while (!stop.load()) {
        samples.push_back(dockerStats());
        std::this_thread::sleep_for(std::chrono::seconds(1));
    }
}

int main(int argc, char* argv[]) {
    for (int i=1; i<argc; i++) {
        std::string a=argv[i];
        if (a=="--base-url"&&i+1<argc) BASE_URL=argv[++i];
        else if (a=="--load-level"&&i+1<argc) LOAD_LEVEL=argv[++i];
        else if (a=="--seed"&&i+1<argc) SEED=std::stoi(argv[++i]);
        else if (a=="--skip-setup") SKIP_SETUP=true;
    }

    auto it=LOAD_LEVELS.find(LOAD_LEVEL);
    if (it==LOAD_LEVELS.end()) { std::cerr<<"Unknown load level: "<<LOAD_LEVEL<<"\n"; return 1; }
    const LoadConfig& cfg=it->second;

    curl_global_init(CURL_GLOBAL_ALL);

    std::cout<<"# Social Network Read-Timeline Benchmark (Issue #48)\n";
    std::cout<<"# load_level="<<LOAD_LEVEL<<" target_rps="<<cfg.target_rps
             <<" duration="<<cfg.duration_sec<<"s warmup="<<cfg.warmup_sec<<"s\n";
    std::cout<<"# workload: 50% user-timeline-read, 40% home-timeline-read, 10% compose\n";
    std::cout.flush();

    if (!SKIP_SETUP) setupUsers(cfg);

    int n_workers=std::min(cfg.target_rps*2, 256);
    std::mt19937 rng(SEED);

    auto launchWorkers=[&](){
        std::vector<std::thread> ws;
        for (int i=0;i<n_workers;i++) ws.emplace_back(workerThread,BASE_URL,cfg.n_users);
        return ws;
    };

    // Warmup
    std::cout<<"# Warming up for "<<cfg.warmup_sec<<"s...\n"; std::cout.flush();
    g_stop.store(true); g_producer_done.store(false);
    { auto ws=launchWorkers(); g_stop.store(false); tokenBucketProducer(cfg,true,rng);
      g_producer_done.store(true); g_queue_cv.notify_all(); for(auto&w:ws)w.join(); }
    { std::lock_guard<std::mutex> lk(g_meas_mutex); g_measurements.clear(); }
    g_producer_done.store(false); g_stop.store(false);

    // Measured run
    std::cout<<"# Measuring for "<<cfg.duration_sec<<"s...\n"; std::cout.flush();
    std::vector<std::pair<double,double>> docker_samples;
    std::atomic<bool> docker_stop{false};
    std::thread docker_thread(dockerPoller,std::ref(docker_samples),std::ref(docker_stop));

    auto wall_start=std::chrono::steady_clock::now();
    { auto ws=launchWorkers(); tokenBucketProducer(cfg,false,rng);
      g_producer_done.store(true); g_queue_cv.notify_all(); for(auto&w:ws)w.join(); }
    double wall_sec=std::chrono::duration<double>(std::chrono::steady_clock::now()-wall_start).count();

    docker_stop.store(true); docker_thread.join();
    docker_samples.push_back(dockerStats());

    std::vector<Measurement> meas;
    { std::lock_guard<std::mutex> lk(g_meas_mutex); meas=g_measurements; }

    std::vector<double> all_read, utl_lat, htl_lat, utl_thrift, htl_thrift, cmp_lat;
    int total_ok=0,total_err=0,utl_ok=0,htl_ok=0,cmp_ok=0;

    for (auto& m:meas) {
        bool ok=(m.status==200);
        if (ok) total_ok++; else total_err++;
        if (m.type==ReqType::USER_TIMELINE) {
            utl_lat.push_back(m.latency_ms); all_read.push_back(m.latency_ms);
            if (ok){utl_ok++;if(m.thrift_ms>0)utl_thrift.push_back(m.thrift_ms);}
        } else if (m.type==ReqType::HOME_TIMELINE) {
            htl_lat.push_back(m.latency_ms); all_read.push_back(m.latency_ms);
            if (ok){htl_ok++;if(m.thrift_ms>0)htl_thrift.push_back(m.thrift_ms);}
        } else {
            cmp_lat.push_back(m.latency_ms);
            if (ok) cmp_ok++;
        }
    }

    auto sAll=all_read;   std::sort(sAll.begin(),sAll.end());
    auto sUtl=utl_lat;    std::sort(sUtl.begin(),sUtl.end());
    auto sHtl=htl_lat;    std::sort(sHtl.begin(),sHtl.end());
    auto sUth=utl_thrift; std::sort(sUth.begin(),sUth.end());
    auto sHth=htl_thrift; std::sort(sHth.begin(),sHth.end());

    double cpu_peak=0,cpu_avg=0,mem_peak=0,mem_avg=0;
    if (!docker_samples.empty()) {
        for (auto& s:docker_samples){cpu_peak=std::max(cpu_peak,s.first);mem_peak=std::max(mem_peak,s.second);}
        cpu_avg=std::accumulate(docker_samples.begin(),docker_samples.end(),0.0,
            [](double a,const std::pair<double,double>& b){return a+b.first;})/docker_samples.size();
        mem_avg=std::accumulate(docker_samples.begin(),docker_samples.end(),0.0,
            [](double a,const std::pair<double,double>& b){return a+b.second;})/docker_samples.size();
    }

    // PRIMARY: combined read p50
    emit("p50_ms",              percentile(sAll,50.0));
    emit("p95_ms",              percentile(sAll,95.0));
    emit("p99_ms",              percentile(sAll,99.0));
    emit("p999_ms",             percentile(sAll,99.9));

    emit("user_timeline_p50_ms",  percentile(sUtl,50.0));
    emit("user_timeline_p90_ms",  percentile(sUtl,90.0));
    emit("user_timeline_p95_ms",  percentile(sUtl,95.0));
    emit("user_timeline_p99_ms",  percentile(sUtl,99.0));
    emit("user_timeline_p999_ms", percentile(sUtl,99.9));

    emit("home_timeline_p50_ms",  percentile(sHtl,50.0));
    emit("home_timeline_p90_ms",  percentile(sHtl,90.0));
    emit("home_timeline_p95_ms",  percentile(sHtl,95.0));
    emit("home_timeline_p99_ms",  percentile(sHtl,99.0));
    emit("home_timeline_p999_ms", percentile(sHtl,99.9));

    // Intermediate latency (Thrift hop from nginx to service)
    emit("user_timeline_thrift_p50_ms",  percentile(sUth,50.0));
    emit("user_timeline_thrift_p95_ms",  percentile(sUth,95.0));
    emit("user_timeline_thrift_p99_ms",  percentile(sUth,99.0));
    emit("user_timeline_thrift_p999_ms", percentile(sUth,99.9));

    emit("home_timeline_thrift_p50_ms",  percentile(sHth,50.0));
    emit("home_timeline_thrift_p95_ms",  percentile(sHth,95.0));
    emit("home_timeline_thrift_p99_ms",  percentile(sHth,99.0));
    emit("home_timeline_thrift_p999_ms", percentile(sHth,99.9));

    double total_req=(double)meas.size();
    emit("throughput_rps",            total_req/wall_sec);
    emit("read_throughput_rps",       (double)(utl_lat.size()+htl_lat.size())/wall_sec);
    emit("user_timeline_rps",         (double)utl_lat.size()/wall_sec);
    emit("home_timeline_rps",         (double)htl_lat.size()/wall_sec);
    emit("success_count",             (double)total_ok);
    emit("error_count",               (double)total_err);
    emit("success_rate",              total_req>0?(double)total_ok/total_req:0.0);
    emit("user_timeline_success_rate",utl_lat.empty()?0.0:(double)utl_ok/utl_lat.size());
    emit("home_timeline_success_rate",htl_lat.empty()?0.0:(double)htl_ok/htl_lat.size());

    emit("cpu_percent",               cpu_peak);
    emit("cpu_percent_avg",           cpu_avg);
    emit("memory_mb",                 mem_peak);
    emit("memory_mb_avg",             mem_avg);
    emit("docker_samples",            (double)docker_samples.size());
    emit("wall_time_sec",             wall_sec);
    emit("total_requests",            total_req);

    std::cout<<"\n# Primary metric (p50_ms): "<<std::fixed<<std::setprecision(2)<<percentile(sAll,50.0)<<"\n";
    std::cout<<"# user_timeline_thrift_p50_ms: "<<percentile(sUth,50.0)<<"\n";
    std::cout<<"# home_timeline_thrift_p50_ms: "<<percentile(sHth,50.0)<<"\n";

    curl_global_cleanup();
    return 0;
}
