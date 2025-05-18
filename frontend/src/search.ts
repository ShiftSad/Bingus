interface SearchResult {
    url: string;
    similarity: number;
    snippet: string;
}

// Wait for the DOM to be fully loaded
document.addEventListener('DOMContentLoaded', () => {
    const searchForm = document.getElementById('search-form') as HTMLFormElement;
    const searchInput = document.getElementById('search-input') as HTMLInputElement;
    const resultsContainer = document.getElementById('search-results') as HTMLDivElement;
    const loadingElement = document.getElementById('loading') as HTMLDivElement;
    const noResultsElement = document.getElementById('no-results') as HTMLDivElement;

    // Get query from URL parameter
    const urlParams = new URLSearchParams(window.location.search);
    const query = urlParams.get('q');

    if (query) {
        // Set the search input value to the query
        searchInput.value = query;
        
        // Perform the search
        performSearch(query);
    } else {
        // No query provided, hide loading and show no results
        loadingElement.classList.add('d-none');
        noResultsElement.classList.remove('d-none');
    }

    // Handle new search submission
    searchForm.addEventListener('submit', (e) => {
        e.preventDefault();
        const newQuery = searchInput.value.trim();
        
        if (newQuery) {
            // Update the URL with the new query
            const newUrl = `${window.location.pathname}?q=${encodeURIComponent(newQuery)}`;
            window.history.pushState({ query: newQuery }, '', newUrl);
            
            // Clear previous results and show loading
            resultsContainer.innerHTML = '';
            noResultsElement.classList.add('d-none');
            loadingElement.classList.remove('d-none');
            
            // Perform the search
            performSearch(newQuery);
        }
    });

    async function performSearch(query: string) {
        try {
            const response = await fetch('http://localhost:3000/search/query', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    query: query,
                    limit: 20
                })
            });

            if (!response.ok) {
                throw new Error(`HTTP error! Status: ${response.status}`);
            }

            const results = await response.json() as SearchResult[];
            
            // Hide loading indicator
            loadingElement.classList.add('d-none');
            
            // Sort results by similarity (highest first)
            results.sort((a, b) => b.similarity - a.similarity);
            
            // Display results or show no results message
            if (results.length > 0) {
                displayResults(results);
            } else {
                noResultsElement.classList.remove('d-none');
            }
        } catch (error) {
            console.error('Error performing search:', error);
            loadingElement.classList.add('d-none');
            
            // Show error message
            resultsContainer.innerHTML = `
                <div class="alert alert-danger" role="alert">
                    An error occurred while searching. Please try again later.
                </div>
            `;
        }
    }

    function displayResults(results: SearchResult[]) {
        // Clear the results container
        resultsContainer.innerHTML = '';
        
        // Create and append result items
        results.forEach(result => {
            // Format similarity score as percentage
            const similarityPercentage = Math.round(result.similarity * 100);
            
            // Create URL display - show domain and path
            const urlObj = new URL(result.url);
            const displayUrl = urlObj.hostname + urlObj.pathname;
            
            // Create a title from the URL if no obvious title in snippet
            const title = result.snippet || urlObj.hostname;
            
            const resultElement = document.createElement('div');
            resultElement.className = 'result-item';
            resultElement.innerHTML = `
                <a href="${result.url}" class="result-url">${displayUrl}</a>
                <a href="${result.url}" class="result-title">
                    ${title}
                    <span class="similarity-badge">${similarityPercentage}% match</span>
                </a>
                <div class="result-snippet">${result.snippet}</div>
            `;
            
            resultsContainer.appendChild(resultElement);
        });
    }
});