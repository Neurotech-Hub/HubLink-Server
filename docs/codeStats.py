import git
import os
from datetime import datetime
import matplotlib.pyplot as plt
from collections import defaultdict
import pandas as pd
from matplotlib import font_manager

# Add Outfit fonts
try:
    # Add Outfit Bold font
    font_path = '/Users/mattgaidica/Library/Fonts/Outfit-Bold.ttf'
    font_manager.fontManager.addfont(font_path)
    plt.rcParams['font.family'] = 'Outfit'
except:
    print("Warning: Could not load Outfit font. Using default font instead.")

def analyze_repository(repo_path):
    """Analyze a single git repository and return its statistics."""
    try:
        repo = git.Repo(repo_path)
        stats = {
            'commit_dates': [],
            'total_commits': 0,
            'authors': set(),
            'files_changed': defaultdict(int)
        }
        
        # Get the default branch
        default_branch = repo.active_branch.name
        
        # Iterate through commits on the default branch
        for commit in repo.iter_commits(default_branch):
            stats['commit_dates'].append(commit.committed_datetime)
            stats['total_commits'] += 1
            stats['authors'].add(commit.author.name)
            
            # Count file changes
            if len(commit.parents) > 0:  # Skip first commit
                diffs = commit.parents[0].diff(commit)
                for diff in diffs:
                    if diff.a_path:
                        stats['files_changed'][diff.a_path] += 1
        
        return stats
    except git.exc.InvalidGitRepositoryError:
        print(f"Error: {repo_path} is not a valid git repository")
        return None
    except git.exc.GitCommandError as e:
        print(f"Error in repository {repo_path}: {str(e)}")
        return None

def plot_commit_history(all_commits):
    """Create a histogram of commits over time."""
    plt.style.use('ggplot')
    
    # Convert all dates to relative months from today
    today = datetime.now()
    dates = [commit.replace(tzinfo=None) for commit in all_commits]
    
    # Find the date range
    oldest_date = min(dates)
    weeks_range = (today - oldest_date).days // 7
    
    # Create figure with colored bars
    plt.figure(figsize=(15, 7))
    
    # Plot histogram and get the histogram data
    n, bins, patches = plt.hist(dates, bins=weeks_range, 
            color='royalblue', alpha=0.7, 
            edgecolor='darkblue', linewidth=1)
    
    # Calculate and plot average line
    avg_commits = len(dates) / len(n)
    plt.axhline(y=avg_commits, color='darkred', linestyle='--', linewidth=2, 
                label=f'Average: {avg_commits:.1f} commits/week')
    
    # Create legend with Outfit font
    legend = plt.legend(fontsize=16)
    
    # Customize the plot with Outfit font
    plt.title('Weekly Commit History Across All Repositories', 
             fontsize=32, pad=30)
    plt.xlabel('Time', fontsize=28, labelpad=20)
    plt.ylabel('Commits per Week', fontsize=28, labelpad=20)
    
    # Set up x-axis with relative time labels
    ax = plt.gca()
    
    # Generate month labels including today
    months = list(pd.date_range(start=oldest_date, end=today, freq='ME'))
    if months[-1].date() != today.date():
        months.append(today)
    
    relative_labels = []
    for date in months[:-1]:
        months_ago = (today.year - date.year) * 12 + today.month - date.month
        relative_labels.append(f'-{months_ago} months')
    relative_labels.append('Today')
    
    # Set ticks and labels with Outfit font
    ax.set_xticks(months)
    ax.set_xticklabels(relative_labels, rotation=0, ha='center', fontsize=24)
    ax.tick_params(axis='y', labelsize=24)
    
    # Customize grid and appearance
    ax.grid(True, axis='y', linestyle='--', alpha=0.5, color='gray')
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    
    # Add more bottom margin to prevent label cutoff
    plt.subplots_adjust(bottom=0.2)
    
    # Save to utils folder
    utils_dir = os.path.dirname(os.path.abspath(__file__))
    output_path = os.path.join(utils_dir, 'commit_history.png')
    plt.savefig(output_path, dpi=300, bbox_inches='tight')
    plt.close()
    
    return output_path

def main():
    # List of repository paths to analyze
    repo_paths = [
        "/Users/mattgaidica/Documents/Software/HubLink-Client",
        "/Users/mattgaidica/Documents/Software/HubLink-Gateway",
        "/Users/mattgaidica/Documents/Software/Hublink-Lambda",
        "/Users/mattgaidica/Documents/Software/HubLink-Server",
        "/Users/mattgaidica/Documents/Arduino-ESP32/libraries/Hublink-Node"
    ]

    all_stats = {
        'total_commits': 0,
        'all_authors': set(),
        'all_commits': [],
        'total_files_changed': defaultdict(int)
    }

    # Analyze each repository
    for repo_path in repo_paths:
        if not os.path.exists(repo_path):
            print(f"Error: Repository path {repo_path} does not exist")
            continue
            
        print(f"\nAnalyzing repository: {repo_path}")
        stats = analyze_repository(repo_path)
        
        if stats:
            all_stats['total_commits'] += stats['total_commits']
            all_stats['all_authors'].update(stats['authors'])
            all_stats['all_commits'].extend(stats['commit_dates'])
            
            for file_path, count in stats['files_changed'].items():
                all_stats['total_files_changed'][file_path] += count

    # Print summary statistics
    print("\nSummary Statistics:")
    print(f"Total number of commits: {all_stats['total_commits']}")
    print(f"Number of unique contributors: {len(all_stats['all_authors'])}")
    print(f"\nMost frequently modified files:")
    for file_path, count in sorted(all_stats['total_files_changed'].items(), 
                                 key=lambda x: x[1], reverse=True)[:5]:
        print(f"{file_path}: {count} modifications")

    # Generate commit history plot
    if all_stats['all_commits']:
        output_path = plot_commit_history(all_stats['all_commits'])
        print(f"\nCommit history plot has been saved as '{output_path}'")

if __name__ == "__main__":
    main()
