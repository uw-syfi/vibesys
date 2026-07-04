#pragma once
#include <vector>
#include <algorithm>
#include <numeric>
#include <cmath>

inline double percentile(std::vector<double>& sorted_vals, double pct) {
    if (sorted_vals.empty()) return 0.0;
    if (pct <= 0.0) return sorted_vals.front();
    if (pct >= 100.0) return sorted_vals.back();
    double idx = (pct/100.0)*(sorted_vals.size()-1);
    size_t lo = (size_t)std::floor(idx);
    size_t hi = lo+1;
    if (hi >= sorted_vals.size()) return sorted_vals.back();
    return sorted_vals[lo] + (idx-lo)*(sorted_vals[hi]-sorted_vals[lo]);
}

inline double vmean(const std::vector<double>& v) {
    if (v.empty()) return 0.0;
    return std::accumulate(v.begin(),v.end(),0.0)/v.size();
}