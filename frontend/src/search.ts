interface SearchResult {
    url: string;
    similarity: number;
    snippet: string;
}

document.addEventListener('DOMContentLoaded', () => {
    const searchForm = document.getElementById('search-form') as HTMLFormElement;
    const searchInput = document.getElementById('search-input') as HTMLInputElement;
    const resultsContainer = document.getElementById('search-results') as HTMLDivElement;
    const loadingElement = document.getElementById('loading') as HTMLDivElement;
    const noResultsElement = document.getElementById('no-results') as HTMLDivElement;
    
    let currentQuery = '';
    let currentStart = 0;
    let isLastPage = false;
    const resultsPerPage = 20;
    
    const urlParams = new URLSearchParams(window.location.search);
    const query = urlParams.get('q');

    if (query) {
        searchInput.value = query;
        currentQuery = query;
        
        performSearch(query, 0);
    } else {
        loadingElement.classList.add('d-none');
        noResultsElement.classList.remove('d-none');
    }

    searchForm.addEventListener('submit', (e) => {
        e.preventDefault();
        const newQuery = searchInput.value.trim();
        
        if (newQuery) {
            const newUrl = `${window.location.pathname}?q=${encodeURIComponent(newQuery)}`;
            window.history.pushState({ query: newQuery }, '', newUrl);
            
            currentQuery = newQuery;
            currentStart = 0;
            isLastPage = false;
            
            resultsContainer.innerHTML = '';
            noResultsElement.classList.add('d-none');
            loadingElement.classList.remove('d-none');
            
            const existingPagination = document.getElementById('pagination-container');
            if (existingPagination) {
                existingPagination.remove();
            }
            
            performSearch(newQuery, 0);
        }
    });

    async function performSearch(query: string, start: number) {
        try {
            const response = await fetch('http://localhost:3000/search/query', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    query: query,
                    limit: resultsPerPage,
                    start: start
                })
            });

            if (!response.ok) {
                throw new Error(`HTTP error! Status: ${response.status}`);
            }

            const results = await response.json() as SearchResult[];
            results.sort((a, b) => b.similarity - a.similarity);
            
            if (results.length < resultsPerPage) {
                isLastPage = true;
            }
            
            if (results.length > 0) {
                if (start === 0) {
                    loadingElement.innerHTML = `
                        <div class="spinner-border text-primary" role="status">
                            <span class="visually-hidden">Loading...</span>
                        </div>
                        <p class="mt-2">Fetching page titles...</p>
                    `;
                } else {
                    const paginationContainer = document.getElementById('pagination-container');
                    if (paginationContainer) {
                        paginationContainer.innerHTML = `
                            <div class="d-flex justify-content-center my-4">
                                <div class="spinner-border text-primary" role="status">
                                    <span class="visually-hidden">Loading...</span>
                                </div>
                                <p class="ms-3 mb-0">Fetching titles...</p>
                            </div>
                        `;
                    }
                }
                
                if (start === 0) {
                    const resultElements = createResultElements(results);
                    
                    const titlePromises = results.map((result, index) => {
                        return fetchPageTitle(result.url, resultElements[index]);
                    });
                    
                    await Promise.allSettled(titlePromises);
                    displayResults(resultElements);
                } else {
                    const resultElements = createResultElements(results);
                    appendResults(resultElements);
                    
                    const titlePromises = results.map((result, index) => {
                        return fetchPageTitle(result.url, resultElements[index]);
                    });
                    
                    await Promise.allSettled(titlePromises);
                }
                
                loadingElement.classList.add('d-none');
                updatePagination();
            } else if (start === 0) {
                loadingElement.classList.add('d-none');
                noResultsElement.classList.remove('d-none');
            } else {
                isLastPage = true;
                updatePagination();
            }
            
            currentStart = start + results.length;
        } catch (error) {
            console.error('Error performing search:', error);
            loadingElement.classList.add('d-none');
            
            if (start === 0) {
                resultsContainer.innerHTML = `
                    <div class="alert alert-danger" role="alert">
                        An error occurred while searching. Please try again later.
                    </div>
                `;
            } else {
                const paginationError = document.createElement('div');
                paginationError.className = 'alert alert-warning mt-3';
                paginationError.textContent = 'Could not load additional results. Please try again.';
                resultsContainer.appendChild(paginationError);
            }
        }
    }

    function createResultElements(results: SearchResult[]): HTMLDivElement[] {
        return results.map(result => {
            const similarityPercentage = Math.round(result.similarity * 100);
            
            try {
                const urlObj = new URL(result.url);
                const displayUrl = urlObj.hostname + urlObj.pathname;
                
                const faviconUrl = `https://www.google.com/s2/favicons?domain=${urlObj.hostname}&sz=32`;
                const initialTitle = result.snippet || urlObj.hostname;
                
                const resultElement = document.createElement('div');
                resultElement.className = 'result-item';
                resultElement.innerHTML = `
                    <div class="result-url-container">
                        <img src="${faviconUrl}" class="result-favicon" alt="" onerror="this.src='data:image/svg+xml,%3Csvg xmlns=\\'http://www.w3.org/2000/svg\\' width=\\'16\\' height=\\'16\\' fill=\\'%23666\\' viewBox=\\'0 0 16 16\\'%3E%3Cpath d=\\'M0 8a8 8 0 1 1 16 0A8 8 0 0 1 0 8zm8-7a7 7 0 0 0-5.468 11.37C3.242 11.226 4.805 10 8 10s4.757 1.225 5.468 2.37A7 7 0 0 0 8 1z\\' fill-rule=\\'evenodd\\'/%3E%3C/svg%3E';">
                        <a href="${result.url}" class="result-url">${displayUrl}</a>
                    </div>
                    <a href="${result.url}" class="result-title" id="title-${encodeURIComponent(result.url)}">
                        ${initialTitle}
                        <span class="similarity-badge">${similarityPercentage}% match</span>
                    </a>
                    <div class="result-snippet">${result.snippet}</div>
                `;
                
                return resultElement;
            } catch (e) {
                console.error("Error processing result:", e, result);
                const resultElement = document.createElement('div');
                resultElement.className = 'result-item';
                resultElement.innerHTML = `
                    <div class="result-url-container">
                        <span class="result-url">${result.url}</span>
                    </div>
                    <a href="${result.url}" class="result-title">
                        ${result.snippet || "Unknown Title"}
                        <span class="similarity-badge">${similarityPercentage}% match</span>
                    </a>
                    <div class="result-snippet">${result.snippet}</div>
                `;
                
                return resultElement;
            }
        });
    }

    function displayResults(resultElements: HTMLDivElement[]) {
        resultsContainer.innerHTML = '';
        appendResults(resultElements);
    }
    
    function appendResults(resultElements: HTMLDivElement[]) {
        resultElements.forEach(element => {
            resultsContainer.appendChild(element);
        });
    }
    
    async function fetchPageTitle(url: string, resultElement: HTMLDivElement): Promise<void> {
        try {
            const proxyUrl = `http://localhost:3000/proxy?url=${encodeURIComponent(url)}`;
                
            const response = await fetch(proxyUrl);
            if (!response.ok) {
                throw new Error('Failed to fetch page');
            }
            
            const data = await response.json();
            if (data && data.title) {
                const title = data.title;
                
                const titleElement = resultElement.querySelector('.result-title');
                if (titleElement && title) {
                    const badge = titleElement.querySelector('.similarity-badge');
                    titleElement.innerHTML = title;
                    if (badge) {
                        titleElement.appendChild(badge);
                    }
                }
            }
        } catch (error) {
            console.error(`Error fetching title for ${url}:`, error);
        }
    }
    
    function updatePagination() {
        let paginationContainer = document.getElementById('pagination-container');
        if (!paginationContainer) {
            paginationContainer = document.createElement('div');
            paginationContainer.id = 'pagination-container';
            paginationContainer.className = 'text-center mt-4 mb-5';
            resultsContainer.after(paginationContainer);
        } else {
            paginationContainer.innerHTML = '';
        }
        
        if (isLastPage) {
            paginationContainer.innerHTML = `
                <div class="text-muted">End of results</div>
            `;
            return;
        }
        
        const loadMoreButton = document.createElement('button');
        loadMoreButton.className = 'btn btn-primary px-4 py-2';
        loadMoreButton.innerHTML = `
            Load More Results
            <i class="bi bi-arrow-down-circle ms-2"></i>
        `;
        
        loadMoreButton.addEventListener('click', () => {
            const pageLoadingIndicator = document.createElement('div');
            pageLoadingIndicator.className = 'd-flex justify-content-center my-4';
            pageLoadingIndicator.innerHTML = `
                <div class="spinner-border text-primary" role="status">
                    <span class="visually-hidden">Loading...</span>
                </div>
            `;
            paginationContainer.innerHTML = '';
            paginationContainer.appendChild(pageLoadingIndicator);
            
            performSearch(currentQuery, currentStart);
        });
        
        paginationContainer.appendChild(loadMoreButton);
    }
});