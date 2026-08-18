"""
欧元汇率30天日涨跌幅分析器
计算最新30天欧元日涨跌幅数据
"""

import json
import os
import glob
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
import pandas as pd


class EuroRateAnalyzer:
    """欧元汇率分析器"""
    
    def __init__(self, cache_dir: str = None):
        """
        初始化分析器
        
        Args:
            cache_dir: 缓存目录路径，默认为当前目录下的exchange_rate_cache
        """
        if cache_dir is None:
            # 获取当前文件所在目录
            current_dir = os.path.dirname(os.path.abspath(__file__))
            self.cache_dir = os.path.join(current_dir, "exchange_rate_cache")
        else:
            self.cache_dir = cache_dir
    
    def load_euro_data(self) -> Dict[str, List[Dict]]:
        """
        加载所有欧元汇率数据
        
        Returns:
            按日期排序的汇率数据字典
        """
        # 查找所有欧元数据文件
        pattern = os.path.join(self.cache_dir, "欧元_*.json")
        files = glob.glob(pattern)
        
        if not files:
            raise FileNotFoundError(f"在 {self.cache_dir} 中未找到欧元数据文件")
        
        data_by_date = {}
        
        for file_path in files:
            try:
                # 从文件名提取日期
                filename = os.path.basename(file_path)
                date_str = filename.replace("欧元_", "").replace(".json", "")
                
                # 解析日期
                date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
                
                # 读取JSON数据
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                # 提取记录
                records = data.get('records', [])
                if records:
                    data_by_date[date_obj] = records
                    
            except Exception as e:
                print(f"警告：无法读取文件 {file_path}: {e}")
                continue
        
        # 按日期排序
        sorted_dates = sorted(data_by_date.keys())
        sorted_data = {date: data_by_date[date] for date in sorted_dates}
        
        print(f"成功加载 {len(sorted_data)} 天的欧元汇率数据")
        return sorted_data
    
    def extract_rate_data(self, records: List[Dict]) -> List[Tuple[datetime, float]]:
        """
        从记录中提取现汇卖出价和时间
        
        Args:
            records: 汇率记录列表
            
        Returns:
            (时间, 现汇卖出价) 的元组列表
        """
        rate_data = []
        
        for record in records:
            try:
                # 提取现汇卖出价
                sell_rate_str = record.get('现汇卖出价', '')
                if not sell_rate_str:
                    continue
                
                sell_rate = float(sell_rate_str)
                
                # 提取发布时间
                time_str = record.get('发布时间', '')
                if not time_str:
                    continue
                
                # 解析时间格式 "2025.09.12 10:30:00"
                time_obj = datetime.strptime(time_str, "%Y.%m.%d %H:%M:%S")
                
                rate_data.append((time_obj, sell_rate))
                
            except (ValueError, TypeError) as e:
                print(f"警告：无法解析记录 {record}: {e}")
                continue
        
        return rate_data
    
    def calculate_daily_average(self, rate_data: List[Tuple[datetime, float]]) -> float:
        """
        计算日平均汇率
        
        Args:
            rate_data: (时间, 汇率) 的元组列表
            
        Returns:
            日平均汇率
        """
        if not rate_data:
            return None
        
        rates = [rate for _, rate in rate_data]
        return sum(rates) / len(rates)
    
    def get_latest_before_10am(self, rate_data: List[Tuple[datetime, float]]) -> Optional[Tuple[datetime, float]]:
        """
        获取10点前的最新汇率数据
        
        Args:
            rate_data: (时间, 汇率) 的元组列表
            
        Returns:
            10点前最新的(时间, 汇率)元组，如果没有则返回None
        """
        if not rate_data:
            return None
        
        # 过滤10点前的数据
        before_10am = [(time, rate) for time, rate in rate_data if time.hour < 10]
        
        if not before_10am:
            return None
        
        # 返回最新的（时间最晚的）
        return max(before_10am, key=lambda x: x[0])
    
    def calculate_daily_changes(self, data_by_date: Dict, days: int = 30) -> List[Dict]:
        """
        计算日涨跌幅
        
        Args:
            data_by_date: 按日期排序的汇率数据
            days: 分析的天数
            
        Returns:
            涨跌幅数据列表
        """
        dates = list(data_by_date.keys())
        if len(dates) < 2:
            raise ValueError("数据不足，需要至少2天的数据")
        
        # 取最新的指定天数
        recent_dates = dates[-days:] if len(dates) > days else dates
        
        results = []
        
        for i, current_date in enumerate(recent_dates):
            if i == 0:
                # 第一天没有前一天数据，跳过
                continue
            
            previous_date = recent_dates[i-1]
            
            try:
                # 处理当前日期的数据
                current_records = data_by_date[current_date]
                current_rate_data = self.extract_rate_data(current_records)
                
                # 获取10点前最新数据
                latest_before_10am = self.get_latest_before_10am(current_rate_data)
                if latest_before_10am is None:
                    print(f"跳过 {current_date}：10点前无数据")
                    continue
                
                current_rate = latest_before_10am[1]
                current_time = latest_before_10am[0]
                
                # 处理前一天的数据
                previous_records = data_by_date[previous_date]
                previous_rate_data = self.extract_rate_data(previous_records)
                
                # 计算前一天的平均汇率
                previous_avg = self.calculate_daily_average(previous_rate_data)
                if previous_avg is None:
                    print(f"跳过 {current_date}：前一天 {previous_date} 无有效数据")
                    continue
                
                # 计算涨跌幅
                change_rate = ((current_rate - previous_avg) / previous_avg) * 100
                
                result = {
                    'date': current_date,
                    'current_rate': current_rate,
                    'current_time': current_time,
                    'previous_avg': previous_avg,
                    'change_rate': change_rate,
                    'change_amount': current_rate - previous_avg
                }
                
                results.append(result)
                
            except Exception as e:
                print(f"处理 {current_date} 时出错: {e}")
                continue
        
        return results
    
    def format_results(self, results: List[Dict]) -> str:
        """
        格式化结果输出
        
        Args:
            results: 涨跌幅数据列表
            
        Returns:
            格式化的字符串
        """
        if not results:
            return "没有可用的涨跌幅数据"
        
        output = []
        output.append("=" * 80)
        output.append("欧元汇率30天日涨跌幅分析报告")
        output.append("=" * 80)
        output.append(f"分析时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        output.append(f"数据天数: {len(results)} 天")
        output.append("")
        output.append("日期\t\t10点前最新价\t昨日平均价\t涨跌额\t\t涨跌幅(%)")
        output.append("-" * 80)
        
        total_change = 0
        positive_days = 0
        
        for result in results:
            date_str = result['date'].strftime('%Y-%m-%d')
            current_rate = result['current_rate']
            previous_avg = result['previous_avg']
            change_amount = result['change_amount']
            change_rate = result['change_rate']
            
            # 统计信息
            total_change += change_rate
            if change_rate > 0:
                positive_days += 1
            
            # 格式化输出
            change_symbol = "+" if change_rate >= 0 else ""
            output.append(f"{date_str}\t{current_rate:.2f}\t\t{previous_avg:.2f}\t\t{change_symbol}{change_amount:.2f}\t\t{change_symbol}{change_rate:.2f}%")
        
        output.append("-" * 80)
        
        # 统计摘要
        avg_change = total_change / len(results) if results else 0
        negative_days = len(results) - positive_days
        
        output.append("")
        output.append("统计摘要:")
        output.append(f"  平均日涨跌幅: {avg_change:+.2f}%")
        output.append(f"  上涨天数: {positive_days} 天")
        output.append(f"  下跌天数: {negative_days} 天")
        output.append(f"  最大单日涨幅: {max(results, key=lambda x: x['change_rate'])['change_rate']:+.2f}%")
        output.append(f"  最大单日跌幅: {min(results, key=lambda x: x['change_rate'])['change_rate']:+.2f}%")
        
        return "\n".join(output)
    
    def analyze(self, days: int = 30) -> str:
        """
        执行完整的分析流程
        
        Args:
            days: 分析的天数
            
        Returns:
            格式化的分析结果
        """
        try:
            # 加载数据
            data_by_date = self.load_euro_data()
            
            # 计算涨跌幅
            results = self.calculate_daily_changes(data_by_date, days)
            
            # 格式化输出
            return self.format_results(results)
            
        except Exception as e:
            return f"分析过程中出现错误: {e}"


def main():
    """主函数"""
    try:
        print("开始分析欧元汇率...")
        analyzer = EuroRateAnalyzer()
        print(f"缓存目录: {analyzer.cache_dir}")
        result = analyzer.analyze(60)
        print(result)
    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
